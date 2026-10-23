from odoo import Command, _, api, fields, models
from odoo.exceptions import UserError, ValidationError
from odoo.tools import float_compare, float_is_zero

# Fields that cannot be changed once the invoice is posted.
KSA_LOCKED_FIELDS = {
    'ksa_retention_percent',
    'ksa_handover_date',
    'ksa_advance_payment_id',
    'ksa_recovery_type',
    'ksa_recovery_percent',
    'ksa_recovery_fixed',
}


class AccountMove(models.Model):
    _inherit = 'account.move'

    # ------------------------------------------------------------------
    # Fields entered by the user on the customer invoice
    # ------------------------------------------------------------------
    ksa_retention_percent = fields.Float(
        string='Retention %', digits=(5, 2), copy=True,
        help="Percentage of the work done (untaxed amount) held back by the client until handover.",
    )
    ksa_handover_date = fields.Date(
        string='Expected Handover Date', copy=False,
        help="Due date of the retention amount. Required when a retention is applied.",
    )
    ksa_advance_payment_id = fields.Many2one(
        'account.payment', string='Advance Payment', copy=False, check_company=True,
        index='btree_not_null',
        help="Advance received from the client, from which part is recovered on this invoice.",
    )
    ksa_recovery_type = fields.Selection(
        [('percent', 'Percentage of work done'), ('fixed', 'Fixed amount')],
        string='Recovery Method', default='percent', copy=True,
    )
    ksa_recovery_percent = fields.Float(string='Recovery %', digits=(5, 2), copy=True)
    ksa_recovery_fixed = fields.Monetary(string='Recovery Amount', currency_field='currency_id', copy=True)

    # ------------------------------------------------------------------
    # Computed amounts (invoice currency)
    # ------------------------------------------------------------------
    ksa_retention_amount = fields.Monetary(
        string='Less: Retention', compute='_compute_ksa_amounts', currency_field='currency_id')
    ksa_advance_recovery_amount = fields.Monetary(
        string='Less: Advance Recovery', compute='_compute_ksa_amounts', currency_field='currency_id')
    ksa_amount_due_now = fields.Monetary(
        string='Amount to Pay Now', compute='_compute_ksa_amounts', currency_field='currency_id')
    ksa_has_deductions = fields.Boolean(compute='_compute_ksa_amounts')
    ksa_advance_remaining = fields.Monetary(
        string='Advance Remaining', compute='_compute_ksa_advance_remaining', currency_field='currency_id')
    ksa_retention_balance = fields.Monetary(
        string='Retention Balance', compute='_compute_ksa_retention_balance', currency_field='currency_id',
        help="Retention still held (not yet released) for this invoice.")

    # ------------------------------------------------------------------
    # Technical links
    # ------------------------------------------------------------------
    ksa_sale_order_id = fields.Many2one(
        'sale.order', string='Project (Sales Order)', compute='_compute_ksa_sale_order_id',
        store=True, recursive=True, index='btree_not_null',
        help="Sales order the invoice was created from. If several, the first one is used.",
    )
    ksa_entry_kind = fields.Selection(
        [('hold', 'Retention / Advance Recovery'),
         ('credit', 'Credit Note Adjustment'),
         ('release', 'Retention Release')],
        string='Retention Entry Type', copy=False, index='btree_not_null', readonly=True,
    )
    ksa_origin_move_id = fields.Many2one(
        'account.move', string='Origin Document', copy=False, readonly=True, index='btree_not_null')
    ksa_hold_move_id = fields.Many2one(
        'account.move', string='Retention / Advance Entry', copy=False, readonly=True)
    ksa_credit_move_id = fields.Many2one(
        'account.move', string='Credit Adjustment Entry', copy=False, readonly=True)
    ksa_credit_ratio = fields.Float(copy=False, digits=(16, 8))
    ksa_credit_retention = fields.Monetary(copy=False, currency_field='currency_id')
    ksa_credit_advance = fields.Monetary(copy=False, currency_field='currency_id')

    # ------------------------------------------------------------------
    # Defaults
    # ------------------------------------------------------------------
    @api.model
    def default_get(self, fields_list):
        res = super().default_get(fields_list)
        if 'ksa_retention_percent' in fields_list and 'ksa_retention_percent' not in res:
            move_type = res.get('move_type') or self.env.context.get('default_move_type')
            company = self.env['res.company'].browse(res.get('company_id')) if res.get('company_id') else self.env.company
            if move_type == 'out_invoice' and company.ksa_retention_account_id:
                res['ksa_retention_percent'] = company.ksa_retention_percent
        return res

    @api.model_create_multi
    def create(self, vals_list):
        # Invoices created by code (e.g. from a sales order) also get the company default.
        for vals in vals_list:
            if vals.get('move_type') == 'out_invoice' and 'ksa_retention_percent' not in vals:
                company = (self.env['res.company'].browse(vals['company_id'])
                           if vals.get('company_id') else self.env.company)
                if company.ksa_retention_account_id and company.ksa_retention_percent:
                    vals['ksa_retention_percent'] = company.ksa_retention_percent
        return super().create(vals_list)

    def write(self, vals):
        if not self.env.context.get('ksa_internal') and KSA_LOCKED_FIELDS & set(vals):
            for move in self:
                if move.state == 'posted' and (move.ksa_hold_move_id or move.ksa_credit_move_id
                                               or move.move_type == 'out_invoice'):
                    raise UserError(_(
                        "The retention / advance settings of the posted invoice %s cannot be changed. "
                        "Reset the invoice to draft first.", move.name))
        return super().write(vals)

    # ------------------------------------------------------------------
    # Computes
    # ------------------------------------------------------------------
    @api.depends('move_type', 'amount_untaxed', 'amount_total', 'currency_id', 'ksa_retention_percent',
                 'ksa_recovery_type', 'ksa_recovery_percent', 'ksa_recovery_fixed', 'ksa_advance_payment_id')
    def _compute_ksa_amounts(self):
        for move in self:
            retention = recovery = 0.0
            currency = move.currency_id or move.company_id.currency_id
            if move.move_type == 'out_invoice' and currency:
                retention = currency.round(move.amount_untaxed * move.ksa_retention_percent / 100.0)
                if move.ksa_advance_payment_id:
                    if move.ksa_recovery_type == 'fixed':
                        recovery = currency.round(move.ksa_recovery_fixed)
                    else:
                        recovery = currency.round(move.amount_untaxed * move.ksa_recovery_percent / 100.0)
            move.ksa_retention_amount = retention
            move.ksa_advance_recovery_amount = recovery
            move.ksa_amount_due_now = move.amount_total - retention - recovery
            move.ksa_has_deductions = bool(retention or recovery)

    @api.depends('ksa_advance_payment_id', 'currency_id', 'invoice_date', 'state')
    def _compute_ksa_advance_remaining(self):
        for move in self:
            if move.ksa_advance_payment_id and move.state == 'draft' and move.currency_id:
                move.ksa_advance_remaining = move._ksa_advance_remaining()
            else:
                move.ksa_advance_remaining = 0.0

    @api.depends('ksa_hold_move_id.line_ids.amount_residual_currency')
    def _compute_ksa_retention_balance(self):
        for move in self:
            lines = move.ksa_hold_move_id.line_ids.filtered(lambda l: l.ksa_line_kind == 'retention')
            move.ksa_retention_balance = sum(lines.mapped('amount_residual_currency'))

    @api.depends('invoice_line_ids.sale_line_ids.order_id', 'reversed_entry_id.ksa_sale_order_id',
                 'ksa_origin_move_id.ksa_sale_order_id')
    def _compute_ksa_sale_order_id(self):
        for move in self:
            orders = move.invoice_line_ids.sale_line_ids.order_id
            move.ksa_sale_order_id = (
                orders[:1]
                or move.reversed_entry_id.ksa_sale_order_id
                or move.ksa_origin_move_id.ksa_sale_order_id
            )

    # ------------------------------------------------------------------
    # Constraints
    # ------------------------------------------------------------------
    @api.constrains('ksa_retention_percent', 'ksa_recovery_percent', 'ksa_recovery_fixed')
    def _check_ksa_values(self):
        for move in self:
            for percent in (move.ksa_retention_percent, move.ksa_recovery_percent):
                if not 0.0 <= percent <= 100.0:
                    raise ValidationError(_("Retention and recovery percentages must be between 0 and 100."))
            if move.ksa_recovery_fixed < 0:
                raise ValidationError(_("The advance recovery amount cannot be negative."))

    @api.constrains('ksa_advance_payment_id')
    def _check_ksa_advance_payment(self):
        for move in self.filtered('ksa_advance_payment_id'):
            payment = move.ksa_advance_payment_id
            if not payment.ksa_is_advance or payment.payment_type != 'inbound' or payment.move_id.state != 'posted':
                raise ValidationError(_("%s is not a posted project advance payment.", payment.display_name))
            if payment.partner_id.commercial_partner_id != move.commercial_partner_id:
                raise ValidationError(_("The advance %(payment)s belongs to another customer.",
                                        payment=payment.display_name))

    @api.constrains('ksa_advance_payment_id', 'ksa_recovery_type', 'ksa_recovery_percent', 'ksa_recovery_fixed',
                    'amount_untaxed')
    def _check_ksa_recovery_within_advance(self):
        """A user cannot recover more than what is left on the advance (checked again when posting)."""
        for move in self.filtered(lambda m: m.state == 'draft' and m.ksa_advance_payment_id and m.move_type == 'out_invoice'):
            remaining = move._ksa_advance_remaining()
            if move.currency_id.compare_amounts(move.ksa_advance_recovery_amount, remaining) > 0:
                raise ValidationError(_(
                    "The advance recovery (%(recovery)s) is more than what is left on the advance "
                    "%(payment)s (%(remaining)s).",
                    recovery=move.currency_id.format(move.ksa_advance_recovery_amount),
                    payment=move.ksa_advance_payment_id.display_name,
                    remaining=move.currency_id.format(remaining)))

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------
    def _ksa_advance_remaining(self):
        self.ensure_one()
        payment = self.ksa_advance_payment_id
        if not payment:
            return 0.0
        return payment._ksa_remaining_in(self.currency_id, self.invoice_date or fields.Date.context_today(self))

    def _ksa_receivable_lines(self):
        return self.line_ids.filtered(lambda l: l.display_type == 'payment_term')

    def _ksa_to_company(self, amount):
        """Convert an amount in invoice currency to company currency using the invoice's own rate."""
        self.ensure_one()
        currency, company_currency = self.currency_id, self.company_id.currency_id
        if currency == company_currency:
            return amount
        recv = self._ksa_receivable_lines()
        total_currency = sum(recv.mapped('amount_currency'))
        total_balance = sum(recv.mapped('balance'))
        if total_currency:
            return company_currency.round(amount * total_balance / total_currency)
        return company_currency.round(currency._convert(
            amount, company_currency, self.company_id, self.invoice_date or self.date))

    def _ksa_line_vals(self, *, account, amount, balance, kind, name, partner, source=None,
                       payment=None, date_maturity=None):
        """Vals of one journal item. ``amount`` / ``balance`` are signed (debit > 0)."""
        self.ensure_one()
        vals = {
            'name': name,
            'account_id': account.id,
            'partner_id': partner.id,
            'currency_id': self.currency_id.id,
            'amount_currency': amount,
            'debit': balance if balance > 0 else 0.0,
            'credit': -balance if balance < 0 else 0.0,
            'ksa_line_kind': kind,
            'ksa_source_move_id': (source or self).id,
            'ksa_advance_payment_id': payment.id if payment else False,
        }
        if date_maturity:
            vals['date_maturity'] = date_maturity
        return vals

    # ------------------------------------------------------------------
    # Posting
    # ------------------------------------------------------------------
    def _ksa_validate_before_post(self):
        for move in self:
            retention, recovery = move.ksa_retention_amount, move.ksa_advance_recovery_amount
            if not (retention or recovery):
                continue
            currency = move.currency_id
            if retention:
                if not move.company_id.ksa_retention_account_id:
                    raise UserError(_("Select the Retention Receivable account in Accounting > Retention & Advance "
                                      "> Settings before posting an invoice with retention."))
                if not move.ksa_handover_date:
                    raise UserError(_("Enter the expected handover date of %s: it is the due date of the retention.",
                                      move.display_name))
            if recovery:
                if not move.ksa_advance_payment_id:
                    raise UserError(_("Select the advance payment to recover from on %s.", move.display_name))
                remaining = move._ksa_advance_remaining()
                if currency.compare_amounts(recovery, remaining) > 0:
                    raise UserError(_(
                        "The advance recovery (%(recovery)s) is more than what is left on the advance "
                        "(%(remaining)s).",
                        recovery=currency.format(recovery), remaining=currency.format(remaining)))
            if currency.compare_amounts(retention + recovery, move.amount_total) > 0:
                raise UserError(_("Retention plus advance recovery cannot be more than the invoice total."))

    def _post(self, soft=True):
        to_validate = self.filtered(lambda m: m.move_type == 'out_invoice' and not m.ksa_hold_move_id)
        to_validate._ksa_validate_before_post()
        posted = super()._post(soft=soft)
        for move in posted.filtered(lambda m: m.move_type == 'out_invoice' and not m.ksa_hold_move_id):
            move._ksa_create_hold_move()
        for move in posted.filtered(
                lambda m: m.move_type == 'out_refund' and m.reversed_entry_id and not m.ksa_credit_move_id):
            move._ksa_create_credit_move()
        return posted

    def _ksa_create_hold_move(self):
        """Create (and reconcile) the new entry that parks the retention and applies the advance.

        Entry (invoice currency amounts; same date as the invoice)::

            Dr Retention Receivable   R   (due = handover date)
               Cr Receivable                R   -> reconciled with the invoice
            Dr Receivable             A        -> reconciled with the advance payment
               Cr Receivable                A   -> reconciled with the invoice
        """
        self.ensure_one()
        retention, recovery = self.ksa_retention_amount, self.ksa_advance_recovery_amount
        if not (retention or recovery):
            return
        company = self.company_id
        recv = self._ksa_receivable_lines()
        if not recv:
            raise UserError(_("The invoice %s has no receivable line.", self.display_name))
        account, partner = recv[0].account_id, recv[0].partner_id
        payment = self.ksa_advance_payment_id
        lines = []
        if retention:
            balance = self._ksa_to_company(retention)
            lines.append(Command.create(self._ksa_line_vals(
                account=company.ksa_retention_account_id, amount=retention, balance=balance,
                kind='retention', name=_("Retention %s", self.name), partner=partner,
                date_maturity=self.ksa_handover_date)))
            lines.append(Command.create(self._ksa_line_vals(
                account=account, amount=-retention, balance=-balance,
                kind='retention_reclass', name=_("Retention %s", self.name), partner=partner)))
        if recovery:
            balance = self._ksa_to_company(recovery)
            lines.append(Command.create(self._ksa_line_vals(
                account=account, amount=recovery, balance=balance, payment=payment,
                kind='advance_apply', name=_("Advance applied on %s", self.name), partner=partner)))
            lines.append(Command.create(self._ksa_line_vals(
                account=account, amount=-recovery, balance=-balance, payment=payment,
                kind='advance_recovery', name=_("Advance recovery %s", self.name), partner=partner)))

        hold = self.env['account.move'].with_company(company).with_context(ksa_internal=True).create({
            'move_type': 'entry',
            'journal_id': company._ksa_get_journal().id,
            'date': self.date,
            'ref': _("Retention / advance recovery of %s", self.name),
            'partner_id': partner.id,
            'company_id': company.id,
            'ksa_entry_kind': 'hold',
            'ksa_origin_move_id': self.id,
            'ksa_sale_order_id': self.ksa_sale_order_id.id,
            'line_ids': lines,
        })
        hold.action_post()

        # 1. the receivable side of the retention + recovery is matched with the invoice
        reclass = hold.line_ids.filtered(lambda l: l.ksa_line_kind in ('retention_reclass', 'advance_recovery'))
        (reclass + recv).reconcile()
        # 2. the advance applied is matched with the advance payment
        if recovery:
            applied = hold.line_ids.filtered(lambda l: l.ksa_line_kind == 'advance_apply')
            (applied + payment._ksa_advance_lines()).reconcile()
            if not float_is_zero(applied.amount_residual, precision_rounding=company.currency_id.rounding):
                raise UserError(_("The advance %s could not be fully matched. Check its remaining amount.",
                                  payment.display_name))
        self.with_context(ksa_internal=True).ksa_hold_move_id = hold

    def _ksa_create_credit_move(self):
        """Give back retention and advance, in proportion, when an invoice is credited.

        Entry (mirror of the original, scaled by credit note / invoice)::

            Dr Receivable                R'   -> reconciled with the credit note
               Cr Retention Receivable        R'   -> reconciled with the retention held
            Dr Receivable                A'   -> reconciled with the credit note
               Cr Receivable                   A'   stays open: it is the advance given back
        """
        self.ensure_one()
        original = self.reversed_entry_id
        hold = original.ksa_hold_move_id
        if original.move_type != 'out_invoice' or not hold:
            return
        company, currency = self.company_id, original.currency_id
        previous = self.search([('reversed_entry_id', '=', original.id),
                                ('ksa_credit_move_id', '!=', False), ('id', '!=', self.id)])
        prior_ratio = sum(previous.mapped('ksa_credit_ratio'))
        if self.currency_id == currency and original.amount_total:
            ratio = self.amount_total / original.amount_total
        else:
            ratio = abs(self.amount_total_signed) / (abs(original.amount_total_signed) or 1.0)
        ratio = min(ratio, 1.0 - prior_ratio)
        if ratio <= 1e-9:
            return
        is_last = float_compare(prior_ratio + ratio, 1.0, precision_digits=6) >= 0

        def part(total, already):
            return currency.round(total - already) if is_last else currency.round(total * ratio)

        retention = part(original.ksa_retention_amount, sum(previous.mapped('ksa_credit_retention')))
        advance = part(original.ksa_advance_recovery_amount, sum(previous.mapped('ksa_credit_advance')))
        credit_vals = {'ksa_credit_ratio': ratio, 'ksa_credit_retention': retention, 'ksa_credit_advance': advance}
        if not (retention or advance):
            self.with_context(ksa_internal=True).write(credit_vals)
            return

        held = hold.line_ids.filtered(lambda l: l.ksa_line_kind == 'retention')
        if retention and currency.compare_amounts(retention, sum(held.mapped('amount_residual_currency'))) > 0:
            raise UserError(_(
                "The retention of %s has already been (partly) released, so it cannot be credited back. "
                "Undo the retention release first.", original.name))

        recv = self._ksa_receivable_lines()
        account, partner = recv[0].account_id, recv[0].partner_id
        payment = original.ksa_advance_payment_id
        lines = []
        if retention:
            if currency.is_zero(retention - sum(held.mapped('amount_residual_currency'))):
                balance = sum(held.mapped('amount_residual'))
            else:
                balance = original._ksa_to_company(retention)
            lines.append(Command.create(original._ksa_line_vals(
                account=company.ksa_retention_account_id, amount=-retention, balance=-balance,
                kind='retention', name=_("Retention credited %s", self.name), partner=partner)))
            lines.append(Command.create(original._ksa_line_vals(
                account=account, amount=retention, balance=balance,
                kind='retention_reclass', name=_("Retention credited %s", self.name), partner=partner)))
        if advance:
            balance = original._ksa_to_company(advance)
            lines.append(Command.create(original._ksa_line_vals(
                account=account, amount=-advance, balance=-balance, payment=payment,
                kind='advance_restore', name=_("Advance restored %s", self.name), partner=partner)))
            lines.append(Command.create(original._ksa_line_vals(
                account=account, amount=advance, balance=balance, payment=payment,
                kind='advance_recovery', name=_("Advance restored %s", self.name), partner=partner)))

        credit = self.env['account.move'].with_company(company).with_context(ksa_internal=True).create({
            'move_type': 'entry',
            'journal_id': company._ksa_get_journal().id,
            'date': self.date,
            'ref': _("Retention / advance adjustment for credit note %s", self.name),
            'partner_id': partner.id,
            'company_id': company.id,
            'ksa_entry_kind': 'credit',
            'ksa_origin_move_id': self.id,
            'ksa_sale_order_id': original.ksa_sale_order_id.id,
            'line_ids': lines,
        })
        credit.action_post()

        # Retention given back is matched with the retention held.
        if retention:
            new_retention = credit.line_ids.filtered(lambda l: l.ksa_line_kind == 'retention')
            (new_retention + held.filtered(lambda l: not l.reconciled)).reconcile()

        # The receivable side must be absorbed by the credit note BEFORE the credit note is used
        # to pay the invoice, so Odoo's automatic credit note <-> invoice matching (done inside
        # _post) is undone and redone in the right order.
        credit_recv = self._ksa_receivable_lines()
        invoice_recv = original._ksa_receivable_lines()
        automatic = credit_recv.matched_debit_ids.filtered(lambda p: p.debit_move_id in invoice_recv)
        automatic.unlink()
        reclass = credit.line_ids.filtered(lambda l: l.ksa_line_kind in ('retention_reclass', 'advance_recovery')
                                           and l.debit)
        (reclass + credit_recv).reconcile()
        open_invoice = invoice_recv.filtered(lambda l: not l.reconciled)
        open_credit = credit_recv.filtered(lambda l: not l.reconciled)
        open_held = hold.line_ids.filtered(
            lambda l: l.ksa_line_kind in ('retention_reclass', 'advance_recovery') and not l.reconciled)
        if open_invoice and (open_credit or open_held):
            (open_invoice + open_credit + open_held).reconcile()

        credit_vals['ksa_credit_move_id'] = credit.id
        self.with_context(ksa_internal=True).write(credit_vals)

    # ------------------------------------------------------------------
    # Reset to draft: undo with reversals, never delete / edit posted entries
    # ------------------------------------------------------------------
    def _ksa_check_can_unwind(self):
        for move in self:
            hold = move.ksa_hold_move_id
            if hold:
                held = hold.line_ids.filtered(lambda l: l.ksa_line_kind == 'retention')
                if held.matched_credit_ids:
                    raise UserError(_(
                        "The retention of %s has been (partly) released or credited. Undo the retention release "
                        "or the credit note before resetting the invoice to draft.", move.name))
                if self.search_count([('reversed_entry_id', '=', move.id), ('ksa_credit_move_id', '!=', False)]):
                    raise UserError(_("The invoice %s has credit notes. Reset them to draft first.", move.name))
            credit = move.ksa_credit_move_id
            if credit:
                restored = credit.line_ids.filtered(lambda l: l.ksa_line_kind == 'advance_restore')
                if restored.matched_debit_ids:
                    raise UserError(_(
                        "The advance given back by %s has already been used on another invoice.", move.name))

    def _ksa_unwind(self):
        for move in self:
            companion = move.ksa_hold_move_id or move.ksa_credit_move_id
            if companion:
                companion._ksa_reverse_companion()
            move.with_context(ksa_internal=True).write({
                'ksa_hold_move_id': False, 'ksa_credit_move_id': False,
                'ksa_credit_ratio': 0.0, 'ksa_credit_retention': 0.0, 'ksa_credit_advance': 0.0,
            })

    def _ksa_reverse_companion(self):
        """Reverse a generated entry (new entry, nothing is deleted) and tag the reversal lines."""
        self.ensure_one()
        reversal = self.with_context(ksa_internal=True)._reverse_moves(
            default_values_list=[{
                'date': fields.Date.context_today(self),
                'ref': _("Reversal of %s", self.name),
            }],
            cancel=True,
        )
        self._ksa_tag_reversal(reversal)
        return reversal

    def _ksa_tag_reversal(self, reversal):
        """Copy the retention / advance tags onto the mirrored lines so reports net to zero."""
        self.ensure_one()
        reversal.with_context(ksa_internal=True).write({
            'ksa_entry_kind': self.ksa_entry_kind,
            'ksa_origin_move_id': self.ksa_origin_move_id.id,
            'ksa_sale_order_id': self.ksa_sale_order_id.id,
        })
        used = self.env['account.move.line']
        for line in self.line_ids.filtered('ksa_line_kind').sorted('id'):
            mirror = reversal.line_ids.filtered(
                lambda l: l not in used and l.account_id == line.account_id
                and float_is_zero(l.balance + line.balance, precision_rounding=line.company_currency_id.rounding)
            )[:1]
            if mirror:
                used |= mirror
                mirror.write({
                    'ksa_line_kind': line.ksa_line_kind,
                    'ksa_source_move_id': line.ksa_source_move_id.id,
                    'ksa_advance_payment_id': line.ksa_advance_payment_id.id,
                })

    def button_draft(self):
        if not self.env.context.get('ksa_internal'):
            for move in self:
                if move.ksa_entry_kind in ('hold', 'credit'):
                    raise UserError(_(
                        "%s was generated automatically. Reset the invoice or credit note it belongs to instead.",
                        move.name))
                if move.ksa_entry_kind == 'release':
                    raise UserError(_("Use 'Undo Retention Release' to cancel a retention release."))
        to_unwind = self.filtered(lambda m: m.ksa_hold_move_id or m.ksa_credit_move_id)
        to_unwind._ksa_check_can_unwind()
        to_unwind._ksa_unwind()
        return super().button_draft()

    def action_ksa_undo_release(self):
        for move in self:
            if move.ksa_entry_kind != 'release' or move.state != 'posted':
                raise UserError(_("Only a posted retention release can be undone."))
            if self.search_count([('reversed_entry_id', '=', move.id)]):
                raise UserError(_("%s has already been reversed.", move.name))
            receivable = move.line_ids.filtered(lambda l: l.ksa_line_kind == 'release_receivable')
            if receivable.matched_credit_ids:
                raise UserError(_(
                    "The released amount of %s has already been (partly) paid. Unreconcile the payment first.",
                    move.name))
            move._ksa_reverse_companion()
        return True
