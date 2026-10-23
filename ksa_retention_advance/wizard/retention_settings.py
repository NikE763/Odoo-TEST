from odoo import _, api, fields, models
from odoo.exceptions import AccessError


class KsaRetentionSettings(models.TransientModel):
    _name = 'ksa.retention.settings'
    _description = 'Retention & Advance Settings'

    company_id = fields.Many2one('res.company', required=True, readonly=True)
    retention_account_id = fields.Many2one(
        'account.account', string='Retention Receivable Account',
        domain="[('account_type', 'in', ('asset_receivable', 'asset_current')), ('reconcile', '=', True)]",
        help="Account on which the retention is parked until handover. It must allow reconciliation.")
    retention_percent = fields.Float(string='Default Retention %', digits=(5, 2))
    retention_journal_id = fields.Many2one(
        'account.journal', string='Journal for Retention Entries',
        domain="[('type', '=', 'general'), ('company_id', '=', company_id)]",
        help="Miscellaneous journal for the generated entries. Empty = first miscellaneous journal.")

    @api.model
    def default_get(self, fields_list):
        res = super().default_get(fields_list)
        company = self.env.company
        res.update({
            'company_id': company.id,
            'retention_account_id': company.ksa_retention_account_id.id,
            'retention_percent': company.ksa_retention_percent,
            'retention_journal_id': company.ksa_retention_journal_id.id,
        })
        return res

    def action_save(self):
        self.ensure_one()
        if not self.env.user.has_group('account.group_account_manager'):
            raise AccessError(_("Only accounting advisers can change the retention settings."))
        # sudo: res.company is writable only by administrators; access was checked just above.
        self.company_id.sudo().write({
            'ksa_retention_account_id': self.retention_account_id.id,
            'ksa_retention_percent': self.retention_percent,
            'ksa_retention_journal_id': self.retention_journal_id.id,
        })
        return {'type': 'ir.actions.act_window_close'}
