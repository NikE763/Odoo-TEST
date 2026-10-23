from odoo import _, api, fields, models
from odoo.exceptions import UserError, ValidationError


class ResCompany(models.Model):
    _inherit = 'res.company'

    ksa_retention_account_id = fields.Many2one(
        'account.account',
        string='Retention Receivable Account',
        domain="[('account_type', 'in', ('asset_receivable', 'asset_current')), ('reconcile', '=', True)]",
        help="Account on which the retention held back by the customer is parked until handover. "
             "It must allow reconciliation.",
    )
    ksa_retention_percent = fields.Float(
        string='Default Retention %', digits=(5, 2),
        help="Pre-filled on new customer invoices. It can be changed on each invoice.",
    )
    ksa_retention_journal_id = fields.Many2one(
        'account.journal',
        string='Retention Journal',
        domain="[('type', '=', 'general'), ('company_id', '=', id)]",
        help="Miscellaneous journal used for the retention / advance / release entries. "
             "If empty, the first miscellaneous journal of the company is used.",
    )

    @api.constrains('ksa_retention_percent')
    def _check_ksa_retention_percent(self):
        for company in self:
            if not 0.0 <= company.ksa_retention_percent <= 100.0:
                raise ValidationError(_("The default retention % must be between 0 and 100."))

    @api.constrains('ksa_retention_account_id')
    def _check_ksa_retention_account(self):
        for company in self:
            account = company.ksa_retention_account_id
            if account and not account.reconcile:
                raise ValidationError(_("The retention account %s must allow reconciliation.", account.display_name))

    def _ksa_get_journal(self):
        self.ensure_one()
        journal = self.ksa_retention_journal_id
        if not journal:
            journal = self.env['account.journal'].search(
                [('company_id', '=', self.id), ('type', '=', 'general')], limit=1)
        if not journal:
            raise UserError(_("No miscellaneous journal found for company %s. Create one or select a Retention "
                              "Journal in the retention settings.", self.name))
        return journal
