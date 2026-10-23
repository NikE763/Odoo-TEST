from odoo import _, api, fields, models
from odoo.exceptions import ValidationError


class AccountPayment(models.Model):
    _inherit = 'account.payment'

    ksa_is_advance = fields.Boolean(
        string='Project Advance', copy=False,
        help="Tick for a customer advance payment received before work starts. "
             "Only such payments can be selected for advance recovery on invoices.",
    )
    ksa_sale_order_id = fields.Many2one(
        'sale.order', string='Project (Sales Order)', copy=False, check_company=True,
        help="Project this advance belongs to. Used to group the advance in the retention report.",
    )
    ksa_advance_remaining = fields.Monetary(
        string='Advance Remaining', compute='_compute_ksa_advance_remaining',
        currency_field='currency_id',
        help="Part of this advance that has not been recovered on invoices yet.",
    )

    @api.constrains('ksa_is_advance', 'payment_type', 'partner_type', 'partner_id')
    def _check_ksa_is_advance(self):
        for payment in self.filtered('ksa_is_advance'):
            if payment.payment_type != 'inbound' or payment.partner_type != 'customer' or not payment.partner_id:
                raise ValidationError(_("A project advance must be an incoming payment from a customer."))

    @api.depends('ksa_is_advance', 'move_id.line_ids.amount_residual', 'move_id.line_ids.amount_residual_currency')
    def _compute_ksa_advance_remaining(self):
        for payment in self:
            if payment.ksa_is_advance and payment.move_id.state == 'posted':
                payment.ksa_advance_remaining = payment._ksa_remaining_in(payment.currency_id, payment.date)
            else:
                payment.ksa_advance_remaining = 0.0

    def _ksa_advance_lines(self):
        """Open credit journal items that make up what is left of this advance.

        = the open credit of the payment itself + the credits re-created when a
        credit note gives back part of an already recovered advance.
        """
        self.ensure_one()
        Line = self.env['account.move.line']
        if self.move_id.state != 'posted':
            return Line
        _liquidity, counterpart, _writeoff = self._seek_for_lines()
        lines = counterpart.filtered(lambda l: not l.reconciled and l.amount_residual < 0)
        restores = Line.search([
            ('ksa_advance_payment_id', '=', self.id),
            ('ksa_line_kind', '=', 'advance_restore'),
            ('parent_state', '=', 'posted'),
            ('reconciled', '=', False),
            ('amount_residual', '<', 0),
        ])
        return lines | restores

    def _ksa_remaining_in(self, currency, date):
        """Remaining advance expressed in ``currency``."""
        self.ensure_one()
        company = self.company_id
        total = 0.0
        for line in self._ksa_advance_lines():
            if line.currency_id == currency:
                total += -line.amount_residual_currency
            else:
                total += company.currency_id._convert(-line.amount_residual, currency, company, date)
        return currency.round(total)
