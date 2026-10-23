{
    'name': 'KSA Contracting - Retention & Advance on Invoices',
    'version': '19.0.1.0.0',
    'category': 'Accounting/Localizations',
    'summary': 'Retention (guarantee) and advance-payment recovery on progress invoices',
    'description': """
Retention and advance recovery for contracting companies
=========================================================
* Retention % held back on each progress invoice, parked on a dedicated
  Retention Receivable account with its own due date (handover date).
* Recovery of a customer advance payment, matched automatically.
* "Release Retention" wizard that moves retention to the normal receivable.
* Credit notes reverse retention / advance in proportion.
* Customer / project report as at any date with Excel export.
""",
    'author': 'Candidate',
    'license': 'LGPL-3',
    'depends': ['account', 'sale', 'l10n_sa'],
    'data': [
        'security/ir.model.access.csv',
        'views/res_company_views.xml',
        'views/account_payment_views.xml',
        'views/account_move_views.xml',
        'views/res_partner_views.xml',
        'wizard/retention_release_wizard_views.xml',
        'wizard/retention_report_views.xml',
        'views/menus.xml',
        'report/invoice_report.xml',
    ],
    'installable': True,
    'application': False,
}
