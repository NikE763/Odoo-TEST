import base64
import io
from collections import defaultdict

from odoo import Command, _, api, fields, models
from odoo.exceptions import UserError

MEASURES = [
    ('work_done', 'Work Done (excl. VAT)'),
    ('total_invoiced', 'Total Invoiced (incl. VAT)'),
    ('retention_held', 'Retention Held'),
    ('retention_released', 'Retention Released'),
    ('retention_balance', 'Retention Balance'),
    ('advance_received', 'Advance Received'),
    ('advance_recovered', 'Advance Recovered'),
    ('advance_balance', 'Advance Balance'),
]


class KsaRetentionReport(models.TransientModel):
    _name = 'ksa.retention.report'
    _description = 'Retention & Advance Report'

    date_as_at = fields.Date(string='As at', required=True, default=fields.Date.context_today)
    company_ids = fields.Many2many(
        'res.company', string='Companies', default=lambda self: self.env.companies)
    partner_ids = fields.Many2many('res.partner', string='Customers', domain="[('parent_id', '=', False)]",
                                   help="Leave empty for all customers.")
    line_ids = fields.One2many('ksa.retention.report.line', 'report_id', string='Lines', readonly=True)
    xlsx_file = fields.Binary(string='Excel File', readonly=True, attachment=False)
    xlsx_filename = fields.Char(readonly=True)

    # ------------------------------------------------------------------
    # Data
    # ------------------------------------------------------------------
    def _get_companies(self):
        self.ensure_one()
        companies = (self.company_ids or self.env.companies) & self.env.user.company_ids
        if not companies:
            raise UserError(_("Select at least one company."))
        return companies

    def _collect(self):
        """Return {(company, customer, sale order): {measure: amount in company currency}}."""
        self.ensure_one()
        companies = self._get_companies()
        self = self.with_context(allowed_company_ids=companies.ids)  # noqa: PLW0642
        as_at = self.date_as_at
        partners = self.partner_ids.commercial_partner_id
        buckets = defaultdict(lambda: dict.fromkeys([m for m, _label in MEASURES], 0.0))
        active = set()

        # 1. Invoiced (customer invoices minus credit notes)
        domain = [('company_id', 'in', companies.ids), ('state', '=', 'posted'),
                  ('move_type', 'in', ('out_invoice', 'out_refund')), ('date', '<=', as_at)]
        if partners:
            domain.append(('commercial_partner_id', 'in', partners.ids))
        for move in self.env['account.move'].search(domain):
            sign = 1 if move.move_type == 'out_invoice' else -1
            key = (move.company_id.id, move.commercial_partner_id.id, move.ksa_sale_order_id.id)
            buckets[key]['total_invoiced'] += sign * abs(move.amount_total_signed)
            buckets[key]['work_done'] += sign * abs(move.amount_untaxed_signed)

        # 2. Retention held / released and advance recovered: from the tagged journal items.
        domain = [('company_id', 'in', companies.ids), ('parent_state', '=', 'posted'), ('date', '<=', as_at),
                  ('ksa_line_kind', 'in', ('retention', 'release_retention', 'advance_apply', 'advance_restore'))]
        if partners:
            domain.append(('partner_id', 'in', partners.ids))
        for line in self.env['account.move.line'].search(domain):
            key = (line.company_id.id, line.partner_id.commercial_partner_id.id,
                   line.ksa_source_move_id.ksa_sale_order_id.id)
            bucket = buckets[key]
            active.add(key)
            if line.ksa_line_kind == 'retention':
                bucket['retention_held'] += line.balance
            elif line.ksa_line_kind == 'release_retention':
                bucket['retention_released'] += -line.balance
            else:  # advance_apply (debit) / advance_restore (credit)
                bucket['advance_recovered'] += line.balance

        # 3. Advance received: posted payments flagged as project advance.
        domain = [('company_id', 'in', companies.ids), ('ksa_is_advance', '=', True),
                  ('move_id.state', '=', 'posted'), ('date', '<=', as_at)]
        if partners:
            domain.append(('partner_id.commercial_partner_id', 'in', partners.ids))
        for payment in self.env['account.payment'].search(domain):
            _liquidity, counterpart, _writeoff = payment._seek_for_lines()
            key = (payment.company_id.id, payment.partner_id.commercial_partner_id.id, payment.ksa_sale_order_id.id)
            buckets[key]['advance_received'] += -sum(counterpart.mapped('balance'))
            active.add(key)

        result = {}
        for key in active:
            bucket = buckets[key]
            bucket['retention_balance'] = bucket['retention_held'] - bucket['retention_released']
            bucket['advance_balance'] = bucket['advance_received'] - bucket['advance_recovered']
            result[key] = bucket
        return result

    def action_compute(self):
        self.ensure_one()
        data = self._collect()
        Company, Partner, Order = self.env['res.company'], self.env['res.partner'], self.env['sale.order']

        def sort_key(key):
            return (Company.browse(key[0]).name or '', Partner.browse(key[1]).name or '',
                    Order.browse(key[2]).name or '')

        commands = [Command.clear()]
        for key in sorted(data, key=sort_key):
            commands.append(Command.create({
                'company_id': key[0], 'partner_id': key[1], 'sale_order_id': key[2] or False, **data[key]}))
        self.line_ids = commands
        return self._reopen()

    def _reopen(self):
        return {
            'type': 'ir.actions.act_window',
            'res_model': self._name,
            'res_id': self.id,
            'view_mode': 'form',
            'target': 'current',
        }

    # ------------------------------------------------------------------
    # Excel export
    # ------------------------------------------------------------------
    def action_export_xlsx(self):
        self.ensure_one()
        import xlsxwriter  # shipped with Odoo (requirements.txt)

        self.action_compute()
        output = io.BytesIO()
        workbook = xlsxwriter.Workbook(output, {'in_memory': True})
        sheet = workbook.add_worksheet(_("Retention & Advance")[:31])
        title = workbook.add_format({'bold': True, 'font_size': 14})
        head = workbook.add_format({'bold': True, 'bg_color': '#714B67', 'font_color': 'white',
                                    'border': 1, 'align': 'center', 'valign': 'vcenter', 'text_wrap': True})
        text = workbook.add_format({'border': 1})
        number = workbook.add_format({'border': 1, 'num_format': '#,##0.00;[Red]-#,##0.00'})
        total_text = workbook.add_format({'bold': True, 'top': 2, 'bg_color': '#EEEEEE'})
        total_num = workbook.add_format({'bold': True, 'top': 2, 'bg_color': '#EEEEEE',
                                         'num_format': '#,##0.00;[Red]-#,##0.00'})

        sheet.write(0, 0, _("Retention & Advance per Customer and Project"), title)
        sheet.write(1, 0, _("As at: %s", fields.Date.to_string(self.date_as_at)))
        headers = [_("Company"), _("Customer"), _("Project (Sales Order)"), _("Currency")] + \
                  [_(label) for _m, label in MEASURES]
        for col, header in enumerate(headers):
            sheet.write(3, col, header, head)
        sheet.set_row(3, 32)
        sheet.set_column(0, 0, 24)
        sheet.set_column(1, 1, 32)
        sheet.set_column(2, 2, 22)
        sheet.set_column(3, 3, 9)
        sheet.set_column(4, 4 + len(MEASURES), 18)

        row = 4
        subtotals = defaultdict(lambda: dict.fromkeys([m for m, _l in MEASURES], 0.0))
        currencies = {}
        for line in self.line_ids:
            sheet.write(row, 0, line.company_id.name, text)
            sheet.write(row, 1, line.partner_id.name or '', text)
            sheet.write(row, 2, line.sale_order_id.name or _("(no project)"), text)
            sheet.write(row, 3, line.currency_id.name, text)
            for col, (measure, _label) in enumerate(MEASURES, start=4):
                sheet.write_number(row, col, line[measure], number)
                subtotals[line.company_id][measure] += line[measure]
            currencies[line.company_id] = line.currency_id.name
            row += 1
        row += 1
        for company, sums in subtotals.items():
            sheet.write(row, 0, _("Total %s", company.name), total_text)
            sheet.write(row, 1, '', total_text)
            sheet.write(row, 2, '', total_text)
            sheet.write(row, 3, currencies[company], total_text)
            for col, (measure, _label) in enumerate(MEASURES, start=4):
                sheet.write_number(row, col, sums[measure], total_num)
            row += 1
        sheet.freeze_panes(4, 3)
        workbook.close()

        filename = "retention_advance_%s.xlsx" % fields.Date.to_string(self.date_as_at)
        self.write({'xlsx_file': base64.b64encode(output.getvalue()), 'xlsx_filename': filename})
        return {
            'type': 'ir.actions.act_url',
            'url': '/web/content/?model=%s&id=%s&field=xlsx_file&filename_field=xlsx_filename&download=true'
                   % (self._name, self.id),
            'target': 'self',
        }


class KsaRetentionReportLine(models.TransientModel):
    _name = 'ksa.retention.report.line'
    _description = 'Retention & Advance Report Line'
    _order = 'id'

    report_id = fields.Many2one('ksa.retention.report', required=True, ondelete='cascade')
    company_id = fields.Many2one('res.company', string='Company', readonly=True)
    currency_id = fields.Many2one(related='company_id.currency_id')
    partner_id = fields.Many2one('res.partner', string='Customer', readonly=True)
    sale_order_id = fields.Many2one('sale.order', string='Project (Sales Order)', readonly=True)
    work_done = fields.Monetary(string='Work Done (excl. VAT)', currency_field='currency_id')
    total_invoiced = fields.Monetary(string='Total Invoiced', currency_field='currency_id')
    retention_held = fields.Monetary(string='Retention Held', currency_field='currency_id')
    retention_released = fields.Monetary(string='Retention Released', currency_field='currency_id')
    retention_balance = fields.Monetary(string='Retention Balance', currency_field='currency_id')
    advance_received = fields.Monetary(string='Advance Received', currency_field='currency_id')
    advance_recovered = fields.Monetary(string='Advance Recovered', currency_field='currency_id')
    advance_balance = fields.Monetary(string='Advance Balance', currency_field='currency_id')
