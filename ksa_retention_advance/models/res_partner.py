from odoo import _, models


class ResPartner(models.Model):
    _inherit = 'res.partner'

    def action_ksa_release_retention(self):
        self.ensure_one()
        return {
            'type': 'ir.actions.act_window',
            'name': _("Release Retention"),
            'res_model': 'ksa.retention.release.wizard',
            'view_mode': 'form',
            'target': 'new',
            'context': {
                'default_partner_id': self.commercial_partner_id.id,
                'default_company_id': self.env.company.id,
            },
        }
