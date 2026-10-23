from odoo import Command, _, api, fields, models
from odoo.exceptions import AccessError, UserError
from odoo.tools import float_compare


class KsaRetentionReleaseWizard(models.TransientModel):
    _name = 'ksa.retention.release.wizard'
    _description = 'Release Retention'

    company_id = fields.Many2one(
        'res.company', required=True, readonly=True, default=lambda self: self.env.company)
    partner_id = fields.Many2one(
        'res.partner', string='Customer', required=True,
        domain="[('parent_id', '=', False)]")
    release_date = fields.Date(
        string='Release Date', required=True, default=fields.Date.context_today,
        help="Date of the release entry and due date of the amount that becomes receivable.")
    release_type = fields.Selection(
        [('full', 'Full retention of the selected invoices'), ('partial', 'Part of the retention')],
        string='Release', required=True, default='full')
    amount = fields.Monetary(string='Amount to Release', currency_field='currency_id')
    currency_id = fields.Many2one('res.currency', compute='_compute_currency_id')
    total_release = fields.Monetary(compute='_compute_total_release', currency_field='currency_id')
    line_ids = fields.One2many(
        'ksa.retention.release.wizard.line', 'wizard_id', string='Invoices',
        compute='_compute_line_ids', store=True, readonly=False)

    # ------------------------------------------------------------------
    @api.depends('line_ids.selected', 'company_id')
    def _compute_currency_id(self):
        for wizard in self:
            selected = wizard.line_ids.filtered('selected')[:1]
            wizard.currency_id = selected.currency_id or wizard.company_id.currency_id

    @api.depends('line_ids.selected', 'line_ids.release_amount')
    def _compute_total_release(self):
        for wizard in self:
            wizard.total_release = sum(wizard.line_ids.filtered('selected').mapped('release_amount'))

    def _get_candidate_lines(self):
        """Retention items still held for the customer (one per invoice)."""
        self.ensure_one()
        if not self.partner_id:
            return self.env['account.move.line']
        return self.env['account.move.line'].search([
            ('company_id', '=', self.company_id.id),
            ('partner_id', '=', self.partner_id.commercial_partner_id.id),
            ('ksa_line_kind', '=', 'retention'),
            ('parent_state', '=', 'posted'),
            ('move_id.ksa_entry_kind', '=', 'hold'),
            ('amount_residual', '>', 0),
        ]).sorted(lambda l: (l.ksa_source_move_id.invoice_date or l.date, l.id))

    @api.depends('partner_id', 'company_id')
    def _compute_line_ids(self):
        for wizard in self:
            commands = [Command.clear()]
            for line in wizard._get_candidate_lines():
                commands.append(Command.create({
                    'retention_line_id': line.id,
                    'selected': False,
                }))
            wizard.line_ids = commands

    # ------------------------------------------------------------------
    def action_release(self):
        self.ensure_one()
        if not self.env.user.has_group('account.group_account_manager'):
            raise AccessError(_("Only accounting advisers can release retention."))
        if self.company_id not in self.env.companies:
            raise UserError(_("Select company %s in the company switcher first.", self.company_id.name))
        lines = self.line_ids.filtered(lambda l: l.selected and l.release_amount > 0)
        if not lines:
            raise UserError(_("Select at least one invoice with a retention balance to release."))
        if self.release_type == 'partial':
            if self.amount <= 0:
                raise UserError(_("Enter the amount to release."))
            if len(lines.currency_id) > 1:
                raise UserError(_("A partial release needs invoices in a single currency. "
                                  "Select invoices of one currency or release them in full."))
            if float_compare(sum(lines.mapped('release_amount')), self.amount,
                             precision_rounding=self.currency_id.rounding) < 0:
                raise UserError(_("The amount to release is more than the retention balance of the selected invoices."))

        company = self.company_id
        journal = company._ksa_get_journal()
        # Odoo would silently postpone an entry dated in a locked period; the accountant must choose.
        lock_date = company._get_user_fiscal_lock_date(journal)
        if lock_date and self.release_date <= lock_date:
            raise UserError(_(
                "The release date %(date)s is in a locked period (locked up to %(lock)s). "
                "Choose a later release date.",
                date=fields.Date.to_string(self.release_date), lock=fields.Date.to_string(lock_date)))
        company_currency = company.currency_id
        move_lines, pairs = [], []
        for line in lines:
            retention = line.retention_line_id
            invoice = line.move_id
            currency = retention.currency_id
            amount = line.release_amount
            residual_currency = retention.amount_residual_currency
            if currency.compare_amounts(amount, residual_currency) > 0:
                raise UserError(_("The amount to release for %(invoice)s is more than its retention balance.",
                                  invoice=invoice.name))
            if currency.is_zero(amount - residual_currency):
                balance = retention.amount_residual
            else:
                balance = company_currency.round(retention.amount_residual * amount / residual_currency)
            receivable = invoice._ksa_receivable_lines()[:1].account_id
            name = _("Retention release %s", invoice.name)
            common = {
                'partner_id': retention.partner_id.id,
                'currency_id': currency.id,
                'name': name,
                'ksa_source_move_id': invoice.id,
            }
            move_lines.append(Command.create({
                **common, 'account_id': retention.account_id.id,
                'amount_currency': -amount, 'debit': 0.0, 'credit': balance,
                'ksa_line_kind': 'release_retention',
            }))
            move_lines.append(Command.create({
                **common, 'account_id': receivable.id,
                'amount_currency': amount, 'debit': balance, 'credit': 0.0,
                'date_maturity': self.release_date,
                'ksa_line_kind': 'release_receivable',
            }))
            pairs.append((invoice, retention))

        move = self.env['account.move'].with_company(company).with_context(ksa_internal=True).create({
            'move_type': 'entry',
            'journal_id': journal.id,
            'date': self.release_date,
            'ref': _("Retention release - %s", self.partner_id.display_name),
            'partner_id': self.partner_id.commercial_partner_id.id,
            'company_id': company.id,
            'ksa_entry_kind': 'release',
            'line_ids': move_lines,
        })
        move.action_post()
        for invoice, retention in pairs:
            released = move.line_ids.filtered(
                lambda l: l.ksa_line_kind == 'release_retention' and l.ksa_source_move_id == invoice)
            (released + retention).reconcile()

        return {
            'type': 'ir.actions.act_window',
            'name': _("Retention Release"),
            'res_model': 'account.move',
            'res_id': move.id,
            'view_mode': 'form',
        }


class KsaRetentionReleaseWizardLine(models.TransientModel):
    _name = 'ksa.retention.release.wizard.line'
    _description = 'Release Retention Line'

    wizard_id = fields.Many2one('ksa.retention.release.wizard', required=True, ondelete='cascade')
    # Only retention_line_id is sent back by the browser on save; the invoice is derived from it.
    retention_line_id = fields.Many2one('account.move.line', required=True)
    move_id = fields.Many2one('account.move', string='Invoice', related='retention_line_id.ksa_source_move_id')
    sale_order_id = fields.Many2one(related='move_id.ksa_sale_order_id', string='Project')
    currency_id = fields.Many2one(related='move_id.currency_id')
    handover_date = fields.Date(related='move_id.ksa_handover_date', string='Handover Date')
    retention_balance = fields.Monetary(
        compute='_compute_retention_balance', currency_field='currency_id', string='Retention Balance')
    selected = fields.Boolean(string='Release')
    release_amount = fields.Monetary(
        compute='_compute_release_amount', currency_field='currency_id', string='To Release')

    @api.depends('retention_line_id.amount_residual_currency')
    def _compute_retention_balance(self):
        for line in self:
            line.retention_balance = line.retention_line_id.amount_residual_currency

    @api.depends('selected', 'retention_balance', 'wizard_id.release_type', 'wizard_id.amount',
                 'wizard_id.line_ids.selected')
    def _compute_release_amount(self):
        """Full: whole balance of each ticked invoice. Partial: the amount is spread oldest invoice first."""
        for line in self:
            line.release_amount = 0.0
        for wizard in self.wizard_id:
            remaining = wizard.amount
            ordered = wizard.line_ids.sorted(lambda l: (l.move_id.invoice_date or l.move_id.date, l.id))
            for line in ordered:
                if not line.selected:
                    line.release_amount = 0.0
                elif wizard.release_type == 'full':
                    line.release_amount = line.retention_balance
                else:
                    taken = min(line.retention_balance, max(remaining, 0.0))
                    line.release_amount = taken
                    remaining -= taken
