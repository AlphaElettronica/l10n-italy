# Author: Andrea Gallina
# ©  2015 Apulia Software srl
# Copyright (C) 2017 Lorenzo Battistini - Agile Business Group
# License AGPL-3.0 or later (http://www.gnu.org/licenses/agpl.html).

import base64
import os
from . import riba_common
from odoo.tests import Form
from odoo.tools import config, safe_eval


class TestInvoiceDueCost(riba_common.TestRibaCommon):

    def setUp(self):
        super(TestInvoiceDueCost, self).setUp()

    def test_add_due_cost(self):
        # ---- Set Service in Company Config
        self.invoice.company_id.due_cost_service_id = self.service_due_cost.id
        # ---- Validate Invoice
        self.invoice.action_invoice_open()
        # ---- Test Invoice has 2 line
        self.assertEqual(len(self.invoice.invoice_line_ids), 3)
        # ---- Test Invoice Line for service cost
        self.assertEqual(self.invoice.invoice_line_ids[1].product_id.id,
                         self.service_due_cost.id)
        # ---- Test Invoice Line for service cost
        self.assertEqual(self.invoice.invoice_line_ids[2].product_id.id,
                         self.service_due_cost.id)
        # ---- Test Cost line is equal to 10.00
        self.assertEqual(
            (self.invoice.invoice_line_ids[1].price_unit +
             self.invoice.invoice_line_ids[2].price_unit), 10.00)
        new_inv = self.invoice.copy()
        self.assertEqual(len(new_inv.invoice_line_ids), 1)

    def test_not_add_due_cost(self):
        # create 2 invoice for partner in same month on the second one no
        # collection fees line expected
        # ---- Set Service in Company Config
        self.invoice.company_id.due_cost_service_id = self.service_due_cost.id
        # ---- Validate Invoice
        self.invoice.action_invoice_open()

        self.invoice2.payment_term_id = self.payment_term2
        self.invoice2.action_invoice_open()
        # ---- Test Invoice has 1 line, no collection fees added because it's added on
        # ---- first due date for partner
        self.assertEqual(len(self.invoice2.invoice_line_ids), 1)

    def test_add_due_cost_same_month(self):
        # create 2 invoice for partner in same month on the second one no
        # collection fees line expected
        self.invoice.partner_id.riba_policy_expenses = 'unlimited'
        # ---- Set Service in Company Config
        self.invoice.company_id.due_cost_service_id = self.service_due_cost.id
        # ---- Validate Invoice with payment 30/60
        self.invoice.action_invoice_open()
        # ---- Validate Invoice with payment 30
        self.invoice2.payment_term_id = self.payment_term2
        self.invoice2.action_invoice_open()
        # ---- Test Invoice 2 has 2 lines (1 for due cost)
        self.assertEqual(len(self.invoice2.invoice_line_ids), 2)

    def test_not_add_due_cost_for_partner_exclude_expense(self):
        # ---- Set Service in Company Config
        self.invoice.company_id.due_cost_service_id = self.service_due_cost.id
        # ---- Exclude expense for partner
        self.invoice.partner_id.riba_exclude_expenses = True
        # ---- Validate Invoice
        self.invoice.action_invoice_open()
        # ---- Test Invoice has 1 line, no collection fees added because
        # ---- the partner is excluded from due costs
        self.assertEqual(len(self.invoice2.invoice_line_ids), 1)

    def test_delete_due_cost_line(self):
        # ---- Set Service in Company Config
        self.invoice.company_id.due_cost_service_id = self.service_due_cost.id
        # ---- Set allow cancel on invoice Journal
        self.invoice.journal_id.update_posted = True
        # ---- Validate Invoice
        self.invoice.action_invoice_open()
        # ---- Cancel Invoice
        self.invoice.action_invoice_cancel()
        self.invoice.action_invoice_draft()
        # ---- Set to Draft
        # Collection fees line has been unlink
        self.assertEqual(len(self.invoice.invoice_line_ids), 1)

    def test_riba_flow(self):
        recent_date = self.env['account.invoice'].search(
            [('date_invoice', '!=', False)], order='date_invoice desc',
            limit=1).date_invoice

        invoice = self.env['account.invoice'].create({
            'date_invoice': recent_date,
            'journal_id': self.sale_journal.id,
            'partner_id': self.partner.id,
            'payment_term_id': self.account_payment_term_riba.id,
            'account_id': self.account_rec1_id.id,
            'invoice_line_ids': [(
                0, 0, {
                    'name': 'product1',
                    'product_id': self.product1.id,
                    'quantity': 1.0,
                    'price_unit': 450.00,
                    'account_id': self.sale_account.id
                }
            )]
        })
        invoice.action_invoice_open()
        riba_move_line_id = False
        for move_line in invoice.move_id.line_ids:
            if move_line.account_id.id == self.account_rec1_id.id:
                riba_move_line_id = move_line.id
                line_ids = self.move_line_model.search([
                    '&',
                    '|',
                    ('riba', '=', 'True'),
                    ('unsolved_invoice_ids', '!=', False),
                    ('account_id.internal_type', '=', 'receivable'),
                    ('reconciled', '=', False),
                    ('distinta_line_ids', '=', False)
                ])
                self.assertEqual(len(line_ids), 1)
                self.assertEqual(line_ids[0].id, move_line.id)
        self.assertTrue(riba_move_line_id)

        # issue wizard
        wizard_riba_issue = self.env['riba.issue'].create({
            'configuration_id': self.riba_config.id
        })
        action = wizard_riba_issue.with_context(
            {'active_ids': [riba_move_line_id]}
        ).create_list()
        riba_list_id = action and action['res_id'] or False
        riba_list = self.distinta_model.browse(riba_list_id)
        riba_list.confirm()
        self.assertEqual(riba_list.state, 'accepted')
        self.assertEqual(invoice.state, 'paid')
        self.assertEqual(len(riba_list.acceptance_move_ids), 1)
        self.assertEqual(len(riba_list.payment_ids), 0)
        riba_list.acceptance_move_ids[0].assert_balanced()

        # I print the C/O slip report
        docargs = {
            'doc_ids': riba_list.ids,
            'doc_model': 'riba.distinta',
            'docs': self.env['riba.distinta'].browse(riba_list.ids),
        }
        data = self.env.ref('l10n_it_ricevute_bancarie.distinta_qweb')\
            .render(docargs)
        if config.get('test_report_directory'):
            open(os.path.join(
                config['test_report_directory'], 'riba-list.' + format
            ), 'wb+').write(data)

        # credit wizard
        wiz_accreditation = self.env['riba.accreditation'].with_context({
            "active_model": "riba.distinta",
            "active_ids": [riba_list_id],
            "active_id": riba_list_id,
        }).create({
            'bank_amount': 445,
            'expense_amount': 5,
        })
        wiz_accreditation.create_move()
        self.assertEqual(riba_list.state, 'accredited')
        riba_list.accreditation_move_id.assert_balanced()

        bank_accreditation_line = False
        for accr_line in riba_list.accreditation_move_id.line_ids:
            if accr_line.account_id.id == self.bank_account.id:
                bank_accreditation_line = accr_line
                break
        self.assertTrue(bank_accreditation_line)

        # register the bank statement with the bank credit
        # st = self.env['account.bank.statement'].create({
        #     'journal_id': self.bank_journal.id,
        #     'name': 'bank statement',
        #     'line_ids': [(0, 0, {
        #         'name': 'C/O',
        #         'amount': 445,
        #     })]
        # })

        # must be possible to close the bank statement line with the
        # credit journal item generated by C/O
        # move_lines_for_rec=st.line_ids[0].get_move_lines_for_reconciliation()
        # self.assertTrue(
        #     bank_accreditation_line.id in [l.id for l in move_lines_for_rec])
        #
        # bank notifies cash in
        bank_move = self.move_model.create({
            'journal_id': self.bank_journal.id,
            'line_ids': [
                (0, 0, {
                    'partner_id': self.partner.id,
                    'account_id': self.sbf_effects.id,
                    'credit': 450,
                    'debit': 0,
                    'name': 'sbf effects',
                }),
                (0, 0, {
                    'partner_id': self.partner.id,
                    'account_id': self.riba_account.id,
                    'credit': 0,
                    'debit': 450,
                    'name': 'Banca conto ricevute bancarie',
                }),
            ]
        })
        to_reconcile = self.env['account.move.line']
        line_set = (
            bank_move.line_ids | riba_list.acceptance_move_ids[0].line_ids)
        for line in line_set:
            if line.account_id.id == self.sbf_effects.id:
                to_reconcile |= line
        self.assertEqual(len(to_reconcile), 2)
        to_reconcile.reconcile()
        # refresh otherwise riba_list.payment_ids is not recomputed
        riba_list.refresh()
        self.assertEqual(riba_list.state, 'paid')
        self.assertEqual(len(riba_list.payment_ids), 1)
        self.assertEqual(len(riba_list.line_ids), 1)
        self.assertEqual(riba_list.line_ids[0].state, 'paid')
        to_reconcile.remove_move_reconcile()
        self.assertEqual(riba_list.state, 'accredited')
        self.assertEqual(riba_list.line_ids[0].state, 'accredited')

    def test_riba_incasso_flow(self):
        """
        RiBa of type 'After Collection' pays invoice when accepted.
        """
        self.invoice.company_id.due_cost_service_id = self.service_due_cost
        self.invoice.action_invoice_open()
        self.assertEqual(self.invoice.state, 'open')

        to_issue_action = self.env.ref('l10n_it_ricevute_bancarie.action_riba_da_emettere')
        to_issue_model = self.env[to_issue_action.res_model]
        to_issue_domain = safe_eval(to_issue_action.domain)
        to_issue_records = to_issue_model.search(to_issue_domain)
        self.assertTrue(to_issue_records)

        issue_wizard_context = {
            'active_model': to_issue_records._name,
            'active_ids': to_issue_records.ids,
        }
        issue_wizard_model = self.env['riba.issue'] \
            .with_context(issue_wizard_context)
        issue_wizard_form = Form(issue_wizard_model)
        issue_wizard_form.configuration_id = self.riba_config_incasso
        issue_wizard = issue_wizard_form.save()
        issue_result = issue_wizard.create_list()

        riba_list_id = issue_result['res_id']
        riba_list_model = issue_result['res_model']
        riba_list = self.env[riba_list_model].browse(riba_list_id)
        riba_list.confirm()

        self.assertEqual(riba_list.state, 'accepted')
        self.assertEqual(self.invoice.state, 'paid')

    def test_unsolved_riba(self):
        # create another invoice to test past due C/O
        recent_date = self.env['account.invoice'].search(
            [('date_invoice', '!=', False)], order='date_invoice desc',
            limit=1).date_invoice
        invoice = self.env['account.invoice'].create({
            'date_invoice': recent_date,
            'journal_id': self.sale_journal.id,
            'partner_id': self.partner.id,
            'payment_term_id': self.account_payment_term_riba.id,
            'account_id': self.account_rec1_id.id,
            'invoice_line_ids': [(
                0, 0, {
                    'name': 'product1',
                    'product_id': self.product1.id,
                    'quantity': 1.0,
                    'price_unit': 100.00,
                    'account_id': self.sale_account.id
                }
            )]
        })
        invoice.action_invoice_open()
        for move_line in invoice.move_id.line_ids:
            if move_line.account_id.id == self.account_rec1_id.id:
                riba_move_line_id = move_line.id
        # issue wizard
        wizard_riba_issue = self.env['riba.issue'].create({
            'configuration_id': self.riba_config.id
        })
        action = wizard_riba_issue.with_context(
            {'active_ids': [riba_move_line_id]}
        ).create_list()
        riba_list_id = action and action['res_id'] or False
        riba_list = self.distinta_model.browse(riba_list_id)
        riba_list.confirm()
        self.assertEqual(riba_list.state, 'accepted')
        self.assertEqual(invoice.state, 'paid')
        # credit wizard
        wiz_accreditation = self.env['riba.accreditation'].with_context({
            "active_model": "riba.distinta",
            "active_ids": [riba_list_id],
            "active_id": riba_list_id,
        }).create({
            'bank_amount': 95,
            'expense_amount': 5,
        })
        wiz_accreditation.create_move()
        self.assertEqual(riba_list.state, 'accredited')
        riba_list.accreditation_move_id.assert_balanced()

        # past due wizard
        wiz_unsolved = self.env['riba.unsolved'].with_context({
            "active_model": "riba.distinta.line",
            "active_ids": [riba_list.line_ids[0].id],
            "active_id": riba_list.line_ids[0].id,
        }).create({
            'bank_amount': 102,
            'expense_amount': 2,
        })
        wiz_unsolved.create_move()
        self.assertEqual(riba_list.state, 'unsolved')
        self.assertEqual(len(riba_list.line_ids), 1)
        self.assertEqual(riba_list.line_ids[0].state, 'unsolved')
        self.assertTrue(invoice.unsolved_move_line_ids)

        self.assertEqual(len(riba_list.unsolved_move_ids), 1)
        bank_unsolved_line = False
        for unsolved_line in riba_list.unsolved_move_ids[0].line_ids:
            if unsolved_line.account_id.id == self.bank_account.id:
                bank_unsolved_line = unsolved_line
                break
        self.assertTrue(bank_unsolved_line)

        # register the bank statement with the bank credit
        # st = self.env['account.bank.statement'].create({
        #     'journal_id': self.bank_journal.id,
        #     'name': 'bank statement',
        #     'line_ids': [(0, 0, {
        #         'name': 'C/O',
        #         'amount': -102,
        #     })]
        # })
        # must be possible to close the bank statement line with the
        # past due journal item generated by C/O
        # move_lines_for_rec=st.line_ids[0].get_move_lines_for_reconciliation()
        # self.assertTrue(
        #     bank_unsolved_line.id in [l.id for l in move_lines_for_rec])

        riba_list.line_ids[0].unsolved_move_id.line_ids.remove_move_reconcile()
        self.assertEqual(riba_list.state, 'accredited')
        self.assertEqual(len(riba_list.line_ids), 1)
        self.assertEqual(riba_list.line_ids[0].state, 'accredited')

    def test_riba_fatturapa(self):
        recent_date = self.env['account.invoice'].search(
            [('date_invoice', '!=', False)], order='date_invoice desc',
            limit=1).date_invoice
        invoice = self.env['account.invoice'].create({
            'date_invoice': recent_date,
            'journal_id': self.sale_journal.id,
            'partner_id': self.partner.id,
            'payment_term_id': self.account_payment_term_riba.id,
            'account_id': self.account_rec1_id.id,
            'invoice_line_ids': [(
                0, 0, {
                    'name': 'product1',
                    'product_id': self.product1.id,
                    'quantity': 1.0,
                    'price_unit': 450.00,
                    'account_id': self.sale_account.id
                }
            )],
            'related_documents': [(
                0, 0, {
                    'type': 'order',
                    'name': 'SO1232',
                    'cig': '7987210EG5',
                    'cup': 'H71N17000690124',
                }
            )],
        })
        invoice.action_invoice_open()
        # issue wizard
        riba_move_line_id = invoice.move_id.line_ids.filtered(
            lambda x: x.account_id == self.account_rec1_id
        )
        wizard_riba_issue = self.env['riba.issue'].create({
            'configuration_id': self.riba_config.id
        })
        action = wizard_riba_issue.with_context(
            {'active_ids': [riba_move_line_id.id]}
        ).create_list()
        riba_list_id = action and action['res_id'] or False
        riba_list = self.distinta_model.browse(riba_list_id)
        riba_list.confirm()
        self.assertEqual(riba_list.line_ids[0].cig, '7987210EG5')
        self.assertEqual(riba_list.line_ids[0].cup, 'H71N17000690124')
        wizard_riba_export = self.env['riba.file.export'].create({})
        wizard_riba_export.with_context(
            {'active_ids': [riba_list.id]}
        ).act_getfile()
        riba_txt = base64.decodebytes(wizard_riba_export.riba_txt)
        self.assertTrue(
            b'CIG: 7987210EG5 CUP: H71N17000690124' in riba_txt
        )

    def test_riba_fatturapa_group(self):
        self.partner.group_riba = True
        recent_date = self.env['account.invoice'].search(
            [('date_invoice', '!=', False)], order='date_invoice desc',
            limit=1).date_invoice
        invoice = self.env['account.invoice'].create({
            'date_invoice': recent_date,
            'journal_id': self.sale_journal.id,
            'partner_id': self.partner.id,
            'payment_term_id': self.account_payment_term_riba.id,
            'account_id': self.account_rec1_id.id,
            'invoice_line_ids': [(
                0, 0, {
                    'name': 'product1',
                    'product_id': self.product1.id,
                    'quantity': 1.0,
                    'price_unit': 450.00,
                    'account_id': self.sale_account.id
                }
            )],
            'related_documents': [(
                0, 0, {
                    'type': 'order',
                    'name': 'SO1232',
                    'cig': '7987210EG5',
                    'cup': 'H71N17000690124',
                }
            )],
        })
        invoice.action_invoice_open()
        invoice1 = self.env['account.invoice'].create({
            'date_invoice': recent_date,
            'journal_id': self.sale_journal.id,
            'partner_id': self.partner.id,
            'payment_term_id': self.account_payment_term_riba.id,
            'account_id': self.account_rec1_id.id,
            'invoice_line_ids': [(
                0, 0, {
                    'name': 'product1',
                    'product_id': self.product1.id,
                    'quantity': 1.0,
                    'price_unit': 450.00,
                    'account_id': self.sale_account.id
                }
            )],
            'related_documents': [(
                0, 0, {
                    'type': 'order',
                    'name': 'SO1232',
                    'cig': '7987210EG5',
                    'cup': 'H71N17000690125',
                }
            )],
        })
        invoice1.action_invoice_open()
        # issue wizard
        riba_move_line_id = invoice.move_id.line_ids.filtered(
            lambda x: x.account_id == self.account_rec1_id
        )
        riba_move_line1_id = invoice1.move_id.line_ids.filtered(
            lambda x: x.account_id == self.account_rec1_id
        )
        wizard_riba_issue = self.env['riba.issue'].create({
            'configuration_id': self.riba_config.id
        })
        action = wizard_riba_issue.with_context(
            {'active_ids': [riba_move_line_id.id, riba_move_line1_id.id]}
        ).create_list()
        riba_list_id = action and action['res_id'] or False
        riba_list = self.distinta_model.browse(riba_list_id)
        riba_list.confirm()
        self.assertTrue(len(riba_list.line_ids), 2)
        wizard_riba_export = self.env['riba.file.export'].create({})
        wizard_riba_export.with_context(
            {'active_ids': [riba_list.id]}
        ).act_getfile()
        riba_txt = base64.decodebytes(wizard_riba_export.riba_txt)
        self.assertTrue(
            b'CIG: 7987210EG5 CUP: H71N17000690124' in riba_txt
        )
        self.assertTrue(
            b'CIG: 7987210EG5 CUP: H71N17000690125' in riba_txt
        )
