from odoo import fields, models


class AccountMoveLine(models.Model):
    _inherit = 'account.move.line'

    ksa_line_kind = fields.Selection(
        [
            ('retention', 'Retention Held'),
            ('retention_reclass', 'Retention Reclass (receivable side)'),
            ('advance_apply', 'Advance Applied'),
            ('advance_recovery', 'Advance Recovery (receivable side)'),
            ('advance_restore', 'Advance Restored'),
            ('release_retention', 'Retention Released'),
            ('release_receivable', 'Retention Release (receivable side)'),
        ],
        string='Retention / Advance Role', copy=False, index='btree_not_null',
    )
    ksa_source_move_id = fields.Many2one(
        'account.move', string='Source Invoice', copy=False, index='btree_not_null',
        help="Customer invoice this retention / advance line belongs to.",
    )
    ksa_advance_payment_id = fields.Many2one(
        'account.payment', string='Advance Payment', copy=False, index='btree_not_null',
    )
