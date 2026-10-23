from odoo import Command, fields
from odoo.exceptions import UserError, ValidationError
from odoo.tests import tagged

from odoo.addons.account.tests.common import AccountTestInvoicingCommon


@tagged('post_install', 'post_install_l10n', '-at_install')
class TestRetentionAdvance(AccountTestInvoicingCommon):
    """The numbers of the task: 100,000 work done, 15% VAT, 10% retention, 20% advance recovery,
    200,000 advance => 85,000 to pay now."""

    country_code = 'SA'

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.company = cls.env.company
        cls.partner = cls.partner_a
        cls.retention_account = cls.env['account.account'].create({
            'name': 'Retention Receivable',
            'code': '119999',
            'account_type': 'asset_receivable',
            'reconcile': True,
        })
        cls.company.write({
            'ksa_retention_account_id': cls.retention_account.id,
            'ksa_retention_percent': 10.0,
        })

    # ------------------------------------------------------------------ helpers
    def _advance(self, amount=200000.0, partner=None, company=None, journal=None, currency=None):
        company = company or self.company
        values = {
            'payment_type': 'inbound',
            'partner_type': 'customer',
            'partner_id': (partner or self.partner).id,
            'amount': amount,
            'date': '2026-01-01',
            'journal_id': (journal or self.company_data['default_journal_bank']).id,
            'ksa_is_advance': True,
        }
        if currency:
            values['currency_id'] = currency.id
        payment = self.env['account.payment'].with_company(company).create(values)
        payment.action_post()
        return payment

    def _invoice(self, price=100000.0, advance=None, recovery_percent=20.0, retention_percent=10.0,
                 date='2026-02-01', handover='2027-01-31', post=True, currency=None, partner=None,
                 company=None, tax=None):
        company = company or self.company
        values = {
            'move_type': 'out_invoice',
            'partner_id': (partner or self.partner).id,
            'invoice_date': date,
            'company_id': company.id,
            'ksa_retention_percent': retention_percent,
            'ksa_handover_date': handover,
            'invoice_line_ids': [Command.create({
                'product_id': self.product_a.id,
                'quantity': 1,
                'price_unit': price,
                'tax_ids': [Command.set((tax or self.tax_sale_a).ids)],
            })],
        }
        if currency:
            values['currency_id'] = currency.id
        if advance:
            values.update(ksa_advance_payment_id=advance.id, ksa_recovery_percent=recovery_percent)
        invoice = self.env['account.move'].with_company(company).create(values)
        if post:
            invoice.action_post()
        return invoice

    def _retention_lines(self, invoice):
        return invoice.ksa_hold_move_id.line_ids.filtered(lambda l: l.ksa_line_kind == 'retention')

    def _retention_account_balance(self):
        lines = self.env['account.move.line'].search([
            ('account_id', '=', self.retention_account.id), ('parent_state', '=', 'posted')])
        return sum(lines.mapped('balance'))

    def _advance_remaining(self, payment):
        payment.invalidate_recordset()
        return payment._ksa_remaining_in(payment.currency_id, fields.Date.today())

    # ------------------------------------------------------------------ 1-4 the example
    def test_01_example_of_the_task(self):
        self.assertEqual(self.tax_sale_a.amount, 15.0)
        advance = self._advance()
        invoice = self._invoice(advance=advance)

        self.assertEqual(invoice.amount_untaxed, 100000.0)
        self.assertEqual(invoice.amount_total, 115000.0)
        self.assertEqual(invoice.ksa_retention_amount, 10000.0)
        self.assertEqual(invoice.ksa_advance_recovery_amount, 20000.0)
        self.assertEqual(invoice.ksa_amount_due_now, 85000.0)
        # what is due on the invoice / in the aged receivable
        self.assertEqual(invoice.amount_residual, 85000.0)

        retention = self._retention_lines(invoice)
        self.assertEqual(retention.balance, 10000.0)
        self.assertEqual(retention.account_id, self.retention_account)
        self.assertEqual(retention.date_maturity, fields.Date.to_date('2027-01-31'))
        self.assertEqual(invoice.ksa_retention_balance, 10000.0)
        # the advance: 20,000 used, 180,000 left
        self.assertEqual(self._advance_remaining(advance), 180000.0)
        # the invoice itself is untouched (VAT on the full value)
        tax_line = invoice.line_ids.filtered(lambda l: l.display_type == 'tax')
        self.assertEqual(sum(tax_line.mapped('balance')), -15000.0)

    def test_02_cannot_recover_more_than_the_advance(self):
        advance = self._advance(amount=50000.0)
        with self.assertRaises(ValidationError):
            self._invoice(price=1000000.0, advance=advance, recovery_percent=20.0)  # 200,000 > 50,000
        # the second invoice may only take what is left
        first = self._invoice(price=200000.0, advance=advance, recovery_percent=20.0)  # 40,000
        self.assertEqual(self._advance_remaining(advance), 10000.0)
        with self.assertRaises(ValidationError):
            self._invoice(price=200000.0, advance=advance, recovery_percent=20.0)  # 40,000 > 10,000
        self.assertTrue(first.ksa_hold_move_id)

    def test_03_fixed_recovery_amount(self):
        advance = self._advance()
        invoice = self._invoice(advance=advance, post=False)
        invoice.write({'ksa_recovery_type': 'fixed', 'ksa_recovery_fixed': 33000.0})
        invoice.action_post()
        self.assertEqual(invoice.ksa_advance_recovery_amount, 33000.0)
        self.assertEqual(invoice.amount_residual, 115000.0 - 10000.0 - 33000.0)

    def test_04_handover_date_and_account_required(self):
        invoice = self._invoice(handover=False, post=False)
        with self.assertRaises(UserError):
            invoice.action_post()
        self.company.ksa_retention_account_id = False
        invoice.ksa_handover_date = '2027-01-31'
        with self.assertRaises(UserError):
            invoice.action_post()

    def test_05_posted_settings_are_locked(self):
        invoice = self._invoice()
        with self.assertRaises(UserError):
            invoice.ksa_retention_percent = 5.0

    # ------------------------------------------------------------------ 7 reset to draft
    def test_07_reset_to_draft_and_repost_does_not_duplicate(self):
        advance = self._advance()
        invoice = self._invoice(advance=advance)
        first_hold = invoice.ksa_hold_move_id

        invoice.button_draft()
        self.assertFalse(invoice.ksa_hold_move_id)
        self.assertEqual(self._retention_account_balance(), 0.0)
        self.assertEqual(self._advance_remaining(advance), 200000.0)
        self.assertEqual(first_hold.state, 'posted')  # never deleted nor edited: it is reversed

        invoice.action_post()
        self.assertEqual(invoice.amount_residual, 85000.0)
        self.assertEqual(self._retention_account_balance(), 10000.0)
        self.assertEqual(self._advance_remaining(advance), 180000.0)
        holds = self.env['account.move'].search([
            ('ksa_entry_kind', '=', 'hold'), ('ksa_origin_move_id', '=', invoice.id), ('reversed_entry_id', '=', False)])
        self.assertEqual(len(holds), 2)  # the old one (reversed) and the new one
        self.assertNotEqual(invoice.ksa_hold_move_id, first_hold)

        # report shows the figures once, not twice
        report = self.env['ksa.retention.report'].create({'date_as_at': fields.Date.today()})
        report.action_compute()
        self.assertEqual(report.line_ids.retention_held, 10000.0)
        self.assertEqual(report.line_ids.advance_recovered, 20000.0)

    # ------------------------------------------------------------------ 5 release
    def test_08_release_retention_partial_then_full(self):
        advance = self._advance()
        invoice = self._invoice(advance=advance)
        wizard = self.env['ksa.retention.release.wizard'].create({
            'partner_id': self.partner.id, 'release_date': '2027-02-01'})
        self.assertEqual(len(wizard.line_ids), 1)
        wizard.line_ids.selected = True
        wizard.write({'release_type': 'partial', 'amount': 4000.0})
        self.assertEqual(wizard.line_ids.release_amount, 4000.0)
        action = wizard.action_release()

        release = self.env['account.move'].browse(action['res_id'])
        self.assertEqual(release.state, 'posted')
        self.assertEqual(release.date, fields.Date.to_date('2027-02-01'))
        receivable = release.line_ids.filtered(lambda l: l.ksa_line_kind == 'release_receivable')
        self.assertEqual(receivable.balance, 4000.0)
        self.assertEqual(receivable.date_maturity, fields.Date.to_date('2027-02-01'))
        self.assertFalse(receivable.reconciled)  # open: the client can pay it
        self.assertEqual(sum(self._retention_lines(invoice).mapped('amount_residual')), 6000.0)
        self.assertEqual(invoice.amount_residual, 85000.0)

        # cannot release more than the balance
        wizard2 = self.env['ksa.retention.release.wizard'].create({
            'partner_id': self.partner.id, 'release_date': '2027-02-01'})
        wizard2.line_ids.selected = True
        wizard2.write({'release_type': 'partial', 'amount': 7000.0})
        with self.assertRaises(UserError):
            wizard2.action_release()

        wizard2.write({'release_type': 'full'})
        self.assertEqual(wizard2.line_ids.release_amount, 6000.0)
        wizard2.action_release()
        self.assertEqual(self._retention_account_balance(), 0.0)

    def test_08b_release_wizard_saved_like_the_browser(self):
        """The browser sends only the editable fields of each line (no invoice, no readonly fields)."""
        invoice = self._invoice()
        retention = self._retention_lines(invoice)
        wizard = self.env['ksa.retention.release.wizard'].create({
            'partner_id': self.partner.id,
            'release_date': '2027-02-01',
            'release_type': 'full',
            'line_ids': [Command.clear(), Command.create({'retention_line_id': retention.id, 'selected': True})],
        })
        self.assertEqual(wizard.line_ids.move_id, invoice)
        self.assertEqual(wizard.line_ids.release_amount, 10000.0)
        wizard.action_release()
        self.assertEqual(self._retention_account_balance(), 0.0)

    def test_09_undo_release(self):
        invoice = self._invoice()
        wizard = self.env['ksa.retention.release.wizard'].create({
            'partner_id': self.partner.id, 'release_date': '2027-02-01'})
        wizard.line_ids.selected = True
        release = self.env['account.move'].browse(wizard.action_release()['res_id'])
        self.assertEqual(self._retention_account_balance(), 0.0)
        release.action_ksa_undo_release()
        self.assertEqual(self._retention_account_balance(), 10000.0)
        self.assertEqual(invoice.ksa_retention_balance, 10000.0)

    def test_10_release_respects_lock_date(self):
        self._invoice()
        self.company.hard_lock_date = '2027-03-31'
        wizard = self.env['ksa.retention.release.wizard'].create({
            'partner_id': self.partner.id, 'release_date': '2027-02-01'})
        wizard.line_ids.selected = True
        with self.assertRaises(UserError):
            wizard.action_release()

    # ------------------------------------------------------------------ 6 credit notes
    def _credit_note(self, invoice, price=None):
        credit = invoice._reverse_moves([{'invoice_date': invoice.invoice_date}], cancel=False)
        if price is not None:
            credit.invoice_line_ids.price_unit = price
        credit.action_post()
        return credit

    def test_11_full_credit_note(self):
        advance = self._advance()
        invoice = self._invoice(advance=advance)
        credit = self._credit_note(invoice)
        self.assertEqual(credit.amount_total, 115000.0)
        self.assertEqual(invoice.amount_residual, 0.0)
        self.assertEqual(credit.amount_residual, 0.0)
        self.assertEqual(self._retention_account_balance(), 0.0)
        self.assertEqual(self._advance_remaining(advance), 200000.0)

        report = self.env['ksa.retention.report'].create({'date_as_at': fields.Date.today()})
        report.action_compute()
        self.assertEqual(report.line_ids.retention_balance, 0.0)
        self.assertEqual(report.line_ids.advance_recovered, 0.0)

    def test_12_partial_credit_note_is_proportional(self):
        advance = self._advance()
        invoice = self._invoice(advance=advance)
        credit = self._credit_note(invoice, price=50000.0)  # half of the work
        self.assertEqual(credit.amount_total, 57500.0)
        self.assertAlmostEqual(credit.ksa_credit_ratio, 0.5)
        self.assertEqual(credit.ksa_credit_retention, 5000.0)
        self.assertEqual(credit.ksa_credit_advance, 10000.0)
        self.assertEqual(self._retention_account_balance(), 5000.0)
        self.assertEqual(self._advance_remaining(advance), 190000.0)
        # 115,000 - 10,000 - 20,000 = 85,000 due; half of it is credited
        self.assertEqual(invoice.amount_residual, 42500.0)

        # second credit note for the rest: the ratio can never go over 100%
        credit2 = self._credit_note(invoice, price=50000.0)
        self.assertEqual(credit2.ksa_credit_retention, 5000.0)
        self.assertEqual(credit2.ksa_credit_advance, 10000.0)
        self.assertEqual(self._retention_account_balance(), 0.0)
        self.assertEqual(self._advance_remaining(advance), 200000.0)
        self.assertEqual(invoice.amount_residual, 0.0)

    def test_13_credit_note_after_release_is_blocked(self):
        invoice = self._invoice()
        wizard = self.env['ksa.retention.release.wizard'].create({
            'partner_id': self.partner.id, 'release_date': '2027-02-01'})
        wizard.line_ids.selected = True
        wizard.action_release()
        credit = invoice._reverse_moves([{'invoice_date': invoice.invoice_date}], cancel=False)
        with self.assertRaises(UserError):
            credit.action_post()

    def test_14_invoice_with_credit_note_cannot_be_reset(self):
        invoice = self._invoice()
        self._credit_note(invoice, price=50000.0)
        with self.assertRaises(UserError):
            invoice.button_draft()

    # ------------------------------------------------------------------ 10 currencies / companies
    def test_15_foreign_currency_invoice(self):
        usd = self.env.ref('base.USD')
        usd.active = True
        self.env['res.currency.rate'].create({
            'currency_id': usd.id, 'name': '2026-02-01', 'rate': 1 / 3.75, 'company_id': self.company.id})
        advance = self._advance()  # 200,000 SAR
        invoice = self._invoice(advance=advance, currency=usd)

        self.assertEqual(invoice.currency_id, usd)
        self.assertEqual(invoice.ksa_retention_amount, 10000.0)
        self.assertEqual(invoice.ksa_advance_recovery_amount, 20000.0)
        self.assertEqual(invoice.amount_residual, 85000.0)  # USD

        retention = self._retention_lines(invoice)
        self.assertEqual(retention.currency_id, usd)
        self.assertEqual(retention.amount_currency, 10000.0)
        self.assertAlmostEqual(retention.balance, 37500.0, places=1)
        # 20,000 USD = 75,000 SAR taken from the 200,000 SAR advance
        self.assertAlmostEqual(self._advance_remaining(advance), 125000.0, places=1)
        self.assertAlmostEqual(advance._ksa_remaining_in(usd, fields.Date.to_date('2026-02-01')), 33333.33, places=0)

        release_wizard = self.env['ksa.retention.release.wizard'].create({
            'partner_id': self.partner.id, 'release_date': '2027-02-01'})
        release_wizard.line_ids.selected = True
        release = self.env['account.move'].browse(release_wizard.action_release()['res_id'])
        receivable = release.line_ids.filtered(lambda l: l.ksa_line_kind == 'release_receivable')
        self.assertEqual(receivable.currency_id, usd)
        self.assertEqual(receivable.amount_currency, 10000.0)
        self.assertAlmostEqual(self._retention_account_balance(), 0.0, places=1)

    def test_16_multi_company(self):
        company_data_2 = self.setup_other_company()
        company2 = company_data_2['company']
        account2 = self.env['account.account'].with_company(company2).create({
            'name': 'Retention Receivable 2', 'code': '119998',
            'account_type': 'asset_receivable', 'reconcile': True})
        company2.write({'ksa_retention_account_id': account2.id, 'ksa_retention_percent': 5.0})

        invoice1 = self._invoice()
        invoice2 = self._invoice(company=company2, retention_percent=5.0, tax=company_data_2['default_tax_sale'])
        self.assertEqual(invoice2.company_id, company2)
        hold2 = invoice2.ksa_hold_move_id
        self.assertEqual(hold2.company_id, company2)
        self.assertEqual(self._retention_lines(invoice2).account_id, account2)
        self.assertEqual(self._retention_lines(invoice1).account_id, self.retention_account)
        self.assertEqual(invoice2.ksa_retention_amount, 5000.0)

        report = self.env['ksa.retention.report'].create({
            'date_as_at': fields.Date.today(), 'company_ids': [Command.set((self.company | company2).ids)]})
        report.with_context(allowed_company_ids=(self.company | company2).ids).action_compute()
        self.assertEqual(len(report.line_ids), 2)
        by_company = {line.company_id: line.retention_balance for line in report.line_ids}
        self.assertEqual(by_company[self.company], 10000.0)
        self.assertEqual(by_company[company2], 5000.0)

    # ------------------------------------------------------------------ 8 report
    def test_17_report_and_excel(self):
        advance = self._advance()
        advance.ksa_sale_order_id = False
        self._invoice(advance=advance)
        self._invoice(advance=advance, date='2026-03-01')
        report = self.env['ksa.retention.report'].create({'date_as_at': '2026-02-15'})
        report.action_compute()
        line = report.line_ids
        self.assertEqual(len(line), 1)
        self.assertEqual(line.total_invoiced, 115000.0)
        self.assertEqual(line.work_done, 100000.0)
        self.assertEqual(line.retention_held, 10000.0)
        self.assertEqual(line.retention_released, 0.0)
        self.assertEqual(line.retention_balance, 10000.0)
        self.assertEqual(line.advance_received, 200000.0)
        self.assertEqual(line.advance_recovered, 20000.0)
        self.assertEqual(line.advance_balance, 180000.0)

        report.date_as_at = '2026-12-31'
        report.action_compute()
        self.assertEqual(report.line_ids.retention_held, 20000.0)
        self.assertEqual(report.line_ids.advance_balance, 160000.0)

        action = report.action_export_xlsx()
        self.assertEqual(action['type'], 'ir.actions.act_url')
        self.assertTrue(report.xlsx_file)
        self.assertTrue(report.xlsx_filename.endswith('.xlsx'))

    # ------------------------------------------------------------------ 9 print
    def test_18_invoice_report_renders_retention(self):
        advance = self._advance()
        invoice = self._invoice(advance=advance)
        html = self.env['ir.actions.report']._render_qweb_html('account.report_invoice', invoice.ids)[0]
        text = html.decode()
        self.assertIn('Amount to Pay Now', text)
        self.assertIn('Retention', text)
        self.assertIn('Advance Recovery', text)
