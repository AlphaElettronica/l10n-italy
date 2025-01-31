# Copyright 2015 Alessandro Camilli (<http://www.openforce.it>)
# Copyright 2018 Lorenzo Battistini - Agile Business Group
# Copyright 2022 ~ 2023 Simone Rubino - TAKOBI
# License AGPL-3.0 or later (http://www.gnu.org/licenses/agpl.html).

from odoo import models, fields, api, _
import odoo.addons.decimal_precision as dp
from odoo.exceptions import ValidationError
from odoo.fields import first
from odoo.tools import float_compare


class AccountFullReconcile(models.Model):
    _inherit = "account.full.reconcile"

    def _get_wt_moves(self):
        moves = self.mapped('reconciled_line_ids.move_id')
        wt_moves = self.env['withholding.tax.move'].search([
            ('wt_account_move_id', 'in', moves.ids)])
        return wt_moves

    @api.model
    def create(self, vals):
        res = super(AccountFullReconcile, self).create(vals)
        wt_moves = res._get_wt_moves()
        for wt_move in wt_moves:
            if wt_move.full_reconcile_id:
                wt_move.action_paid()
        return res

    @api.multi
    def unlink(self):
        for rec in self:
            wt_moves = rec._get_wt_moves()
            super(AccountFullReconcile, rec).unlink()
            for wt_move in wt_moves:
                if not wt_move.full_reconcile_id:
                    wt_move.action_set_to_draft()
        return True


class AccountPartialReconcile(models.Model):
    _inherit = "account.partial.reconcile"

    def _wt_get_move_lines(self, vals):
        move_line_model = self.env['account.move.line']
        debit_move = move_line_model.browse(vals.get('debit_move_id'))
        credit_move = move_line_model.browse(vals.get('credit_move_id'))
        move_lines = debit_move | credit_move
        return move_lines

    def _wt_get_paying_invoice(self, move_lines):
        moves = move_lines.mapped('move_id')
        invoices = self.env['account.invoice'].search(
            [
                ('move_id', 'in', moves.ids),
            ],
        )
        # If we are reconciling a vendor bill and its refund,
        # we do not need to generate Withholding Tax Moves
        # or change the reconciliation amount
        in_refunding = len(invoices) == 2 \
            and set(invoices.mapped('type')) == {'in_invoice', 'in_refund'}
        if not in_refunding:
            paying_invoice = first(invoices)
        else:
            paying_invoice = self.env['account.invoice'].browse()
        return paying_invoice

    def _get_wt_payment_lines(self):
        """
        Get Payment Lines linked to `self`.

        The Payment Lines can be linked to the Invoice reconciled by `self`.
        """
        self.ensure_one()
        reconciled_move_lines = self.credit_move_id | self.debit_move_id
        reconciled_moves = reconciled_move_lines.mapped('move_id')
        wt_move_lines = self.env['account.move.line'].search(
            [
                (
                    'withholding_tax_generated_by_move_id',
                    'in',
                    reconciled_moves.ids,
                ),
            ],
        )
        return wt_move_lines

    def _get_line_to_reconcile(self, wt_move):
        """
        Get which line of the Journal Entry `wt_move` can be reconciled.
        """
        reconcilable_account_types = ['payable', 'receivable']
        for line in wt_move.line_ids:
            account_type = line.account_id.user_type_id.type
            if account_type in reconcilable_account_types \
               and line.partner_id:
                line_to_reconcile = line
                break
        else:
            line_to_reconcile = self.env['account.move.line'].browse()
        return line_to_reconcile

    def _reconcile_wt_payment(self, invoice, wt_move):
        """
        Reconcile the WT payment `wt_move` in `invoice`.

        :return: The created Reconciliation
        """
        line_to_reconcile = self._get_line_to_reconcile(wt_move)

        if invoice.type in ['in_refund', 'out_invoice']:
            debit_move = self.debit_move_id
            credit_move = line_to_reconcile
        else:
            debit_move = line_to_reconcile
            credit_move = self.credit_move_id
        return self.with_context(no_generate_wt_move=True).create({
            'debit_move_id': debit_move.id,
            'credit_move_id': credit_move.id,
            'amount': abs(wt_move.amount),
        })

    def _create_reconcile_wt_payment(self, invoice):
        """
        Create a WT payment for `invoice`'s WT lines and reconcile it.

        Create the WT Move in the WT Statement.
        If a WT reconciled payment already exists, do nothing.
        If a WT payment already exists, just reconcile it.
        Otherwise, create and reconcile the WT payment.
        `self` is the invoice's reconciliation.

        :param invoice: Invoice containing the WT lines
        """
        reconciled_move_lines = self.credit_move_id | self.debit_move_id
        is_wt_move = any(
            ml.withholding_tax_generated_by_move_id
            for ml in reconciled_move_lines
        )
        # Avoid re-generate wt moves if the move line is a wt move.
        # It's possible if the user unreconciles a wt move under invoice
        generate_moves = not is_wt_move \
            and not self._context.get('no_generate_wt_move')

        wt_moves = self.env['withholding.tax.move'].browse()
        if generate_moves:
            # Wt moves creation
            wt_moves = self.generate_wt_moves()

        # Retrieve any WT payments linked to the invoice
        wt_move_lines = self._get_wt_payment_lines()
        reconciled_wt_move_lines = wt_move_lines.filtered('reconciled')
        if not reconciled_wt_move_lines:
            unreconciled_wt_move_lines = wt_move_lines - reconciled_wt_move_lines
            if unreconciled_wt_move_lines:
                # Reconcile only the first existing WT payment
                wt_move = first(unreconciled_wt_move_lines.mapped('move_id'))
                self._reconcile_wt_payment(invoice, wt_move)
            else:
                for wt_move in wt_moves:
                    wt_move.generate_account_move()

    @api.model
    def create(self, vals):
        # In case of WT The amount of reconcile mustn't exceed the tot net
        # amount. The amount residual will be full reconciled with amount net
        # and amount wt created with payment
        move_lines = self._wt_get_move_lines(vals)
        paying_invoice = self._wt_get_paying_invoice(move_lines)
        # Limit value of reconciliation
        if paying_invoice \
           and paying_invoice.withholding_tax \
           and paying_invoice.amount_net_pay:
            # We must consider amount in foreign currency, if present
            # Note that this is always executed, for every reconciliation.
            # Thus, we must not change amount when not in withholding tax case
            amount = vals.get('amount_currency') or vals.get('amount')
            digits_rounding_precision = paying_invoice.company_id.currency_id.rounding
            if float_compare(
                amount,
                paying_invoice.amount_net_pay,
                precision_rounding=digits_rounding_precision
            ) == 1:
                vals.update({'amount': paying_invoice.amount_net_pay})

        # Create reconciliation
        reconcile = super(AccountPartialReconcile, self).create(vals)

        if paying_invoice.withholding_tax_line_ids:
            reconcile._create_reconcile_wt_payment(paying_invoice)

        return reconcile

    def _prepare_wt_move(self, vals):
        """
        Hook to change values before wt move creation
        """
        return vals

    def _get_wt_statement(self):
        """
        Search WT Statement linked to this Reconciliation.

        :return: The WT statement
            and the line of the Reconciliation is involved in it.
        """
        rec_move_lines = self.debit_move_id | self.credit_move_id
        wt_statement_obj = self.env['withholding.tax.statement']
        for rec_line in rec_move_lines:
            domain = [('move_id', '=', rec_line.move_id.id)]
            wt_statements = wt_statement_obj.search(domain)
            if wt_statements:
                rec_line_statement = rec_line
                break
        else:
            wt_statements = wt_statement_obj.browse()
            rec_line_statement = self.env['account.move.line'].browse()
        return wt_statements, rec_line_statement

    @api.model
    def generate_wt_moves(self):
        # Search statements of competence
        wt_statements, rec_line_statement = self._get_wt_statement()

        # Search payment move line
        rec_move_lines = self.debit_move_id | self.credit_move_id
        rec_line_payment = rec_move_lines - rec_line_statement

        # Generate wt moves
        wt_tax_move_model = self.env['withholding.tax.move']
        wt_moves = wt_tax_move_model.browse()
        for wt_st in wt_statements:
            amount_wt = wt_st.get_wt_competence(self.amount)
            # Date maturity
            p_date_maturity = False
            payment_lines = wt_st.withholding_tax_id.payment_term.compute(
                amount_wt,
                rec_line_payment.date or False)
            if payment_lines and payment_lines[0]:
                p_date_maturity = payment_lines[0][0][0]
            wt_move_vals = {
                'statement_id': wt_st.id,
                'date': rec_line_payment.date,
                'partner_id': rec_line_statement.partner_id.id,
                'reconcile_partial_id': self.id,
                'payment_line_id': rec_line_payment.id,
                'credit_debit_line_id': rec_line_statement.id,
                'withholding_tax_id': wt_st.withholding_tax_id.id,
                'account_move_id': rec_line_payment.move_id.id or False,
                'date_maturity':
                    p_date_maturity or rec_line_payment.date_maturity,
                'amount': amount_wt
            }
            wt_move_vals = self._prepare_wt_move(wt_move_vals)
            wt_move = wt_tax_move_model.create(wt_move_vals)
            wt_moves |= wt_move
        return wt_moves

    @api.multi
    def unlink(self):
        statements = []
        for rec in self:
            # To avoid delete if the wt move are paid
            domain = [('reconcile_partial_id', '=', rec.id),
                      ('state', '!=', 'due')]
            wt_moves = self.env['withholding.tax.move'].search(domain)
            if wt_moves:
                raise ValidationError(
                    _('Warning! Only Withholding Tax moves in Due status \
                    can be deleted'))
            # Statement to recompute
            domain = [('reconcile_partial_id', '=', rec.id)]
            wt_moves = self.env['withholding.tax.move'].search(domain)
            for wt_move in wt_moves:
                if wt_move.statement_id not in statements:
                    statements.append(wt_move.statement_id)

        res = super(AccountPartialReconcile, self).unlink()
        # Recompute statement values
        for st in statements:
            st._compute_total()
        return res


class AccountMove(models.Model):
    _inherit = "account.move"

    @api.one
    def _prepare_wt_values(self):
        partner = False
        wt_competence = {}
        # First : Partner and WT competence
        for line in self.line_id:
            if line.partner_id:
                partner = line.partner_id
                if partner.property_account_position:
                    for wt in (
                        partner.property_account_position.withholding_tax_ids
                    ):
                        wt_competence[wt.id] = {
                            'withholding_tax_id': wt.id,
                            'partner_id': partner.id,
                            'date': self.date,
                            'account_move_id': self.id,
                            'wt_account_move_line_id': False,
                            'base': 0,
                            'amount': 0,
                        }
                break
        # After : Loking for WT lines
        wt_amount = 0
        for line in self.line_id:
            domain = []
            # WT line
            if line.credit:
                domain.append(
                    ('account_payable_id', '=', line.account_id.id)
                )
                amount = line.credit
            else:
                domain.append(
                    ('account_receivable_id', '=', line.account_id.id)
                )
                amount = line.debit
            wt_ids = self.pool['withholding.tax'].search(
                self.env.cr, self.env.uid, domain)
            if wt_ids:
                wt_amount += amount
                if (
                    wt_competence and wt_competence[wt_ids[0]] and
                    'amount' in wt_competence[wt_ids[0]]
                ):
                    wt_competence[wt_ids[0]]['wt_account_move_line_id'] = (
                        line.id)
                    wt_competence[wt_ids[0]]['amount'] = wt_amount
                    wt_competence[wt_ids[0]]['base'] = (
                        self.pool['withholding.tax'].get_base_from_tax(
                            self.env.cr, self.env.uid, wt_ids[0], wt_amount)
                    )

        wt_codes = []
        if wt_competence:
            for key, val in wt_competence.items():
                wt_codes.append(val)
        res = {
            'partner_id': partner and partner.id or False,
            'move_id': self.id,
            'invoice_id': False,
            'date': self.date,
            'base': wt_codes and wt_codes[0]['base'] or 0,
            'tax': wt_codes and wt_codes[0]['amount'] or 0,
            'withholding_tax_id': (
                wt_codes and wt_codes[0]['withholding_tax_id'] or False),
            'wt_account_move_line_id': (
                wt_codes and wt_codes[0]['wt_account_move_line_id'] or False),
            'amount': wt_codes[0]['amount'],
        }
        return res


class AccountAbstractPayment(models.AbstractModel):
    _inherit = "account.abstract.payment"

    @api.model
    def default_get(self, fields):
        """
        Compute amount to pay proportionally to amount total - wt
        """
        rec = super(AccountAbstractPayment, self).default_get(fields)
        invoice_defaults = self.resolve_2many_commands('invoice_ids',
                                                       rec.get('invoice_ids'))
        if invoice_defaults and len(invoice_defaults) == 1:
            invoice = invoice_defaults[0]
            if 'withholding_tax_amount' in invoice \
                    and invoice['withholding_tax_amount']:
                coeff_net = invoice['residual'] / invoice['amount_total']
                rec['amount'] = invoice['amount_net_pay_residual'] * coeff_net
        return rec

    @api.multi
    def _compute_payment_amount(self, invoices=None, currency=None):
        if not invoices:
            invoices = self.invoice_ids
        original_values = {}
        for invoice in invoices:
            if invoice.withholding_tax:
                original_values[invoice] = invoice.residual_signed
                invoice.residual_signed = invoice.amount_net_pay_residual
        res = super(AccountAbstractPayment, self)._compute_payment_amount(
            invoices, currency)
        for invoice in original_values:
            invoice.residual_signed = original_values[invoice]
        return res


class AccountMoveLine(models.Model):
    _inherit = "account.move.line"

    withholding_tax_id = fields.Many2one(
        'withholding.tax', string='Withholding Tax')
    withholding_tax_base = fields.Float(string='Withholding Tax Base')
    withholding_tax_amount = fields.Float(string='Withholding Tax Amount')
    withholding_tax_generated_by_move_id = fields.Many2one(
        'account.move', string='Withholding Tax generated from', readonly=True)

    @api.multi
    def remove_move_reconcile(self):
        # When unreconcile a payment with a wt move linked, it will be
        # unreconciled also the wt account move
        for account_move_line in self:
            rec_move_ids = self.env['account.partial.reconcile']
            domain = self._get_wt_mls_domain(account_move_line)
            wt_mls = self.env['account.move.line'].search(domain)
            # Avoid wt move not in due state
            domain = [('wt_account_move_id', 'in',
                       wt_mls.mapped('move_id').ids)]
            wt_moves = self.env['withholding.tax.move'].search(domain)
            wt_moves.check_unlink()

            for wt_ml in wt_mls:
                rec_move_ids += wt_ml.matched_debit_ids
                rec_move_ids += wt_ml.matched_credit_ids
            rec_move_ids.unlink()
            # Delete wt move
            for wt_move in wt_mls.mapped('move_id'):
                wt_move.button_cancel()
                wt_move.unlink()

        return super(AccountMoveLine, self).remove_move_reconcile()

    def _get_wt_mls_domain(self, account_move_line):
        """
        Get domain to filter account move lines generated by withholding tax moves.
        """
        domain = [
            (
                'withholding_tax_generated_by_move_id',
                '=',
                account_move_line.move_id.id
            ),
        ]
        return domain


class AccountReconciliation(models.AbstractModel):
    _inherit = 'account.reconciliation.widget'

    @api.multi
    def _prepare_move_lines(
        self, move_lines, target_currency=False, target_date=False,
        recs_count=0
    ):
        """
        Net amount for invoices with withholding tax
        """
        res = super(
            AccountReconciliation, self
        )._prepare_move_lines(
            move_lines, target_currency, target_date, recs_count)
        for dline in res:
            if 'id' in dline and dline['id']:
                line = self.env['account.move.line'].browse(dline['id'])
                if line.withholding_tax_amount:
                    dline['debit'] = (
                        line.invoice_id.amount_net_pay_residual if line.debit
                        else 0
                    )
                    dline['credit'] = (
                        line.invoice_id.amount_net_pay_residual
                        if line.credit else 0
                    )
                    dline['name'] += (
                        _(' (Residual Net to pay: %s)')
                        % (dline['debit'] or dline['credit'])
                    )
        return res


class AccountFiscalPosition(models.Model):
    _inherit = "account.fiscal.position"

    withholding_tax_ids = fields.Many2many(
        'withholding.tax', 'account_fiscal_position_withholding_tax_rel',
        'fiscal_position_id', 'withholding_tax_id', string='Withholding Tax')


class AccountInvoice(models.Model):
    _inherit = "account.invoice"

    @api.multi
    @api.depends(
        'invoice_line_ids.price_subtotal', 'withholding_tax_line_ids.tax',
        'currency_id', 'company_id', 'date_invoice', 'payment_move_line_ids')
    def _amount_withholding_tax(self):
        dp_obj = self.env['decimal.precision']
        for invoice in self:
            withholding_tax_amount = 0.0
            for wt_line in invoice.withholding_tax_line_ids:
                withholding_tax_amount += round(
                    wt_line.tax, dp_obj.precision_get('Account'))
            invoice.amount_net_pay = invoice.amount_total - \
                withholding_tax_amount
            amount_net_pay_residual = invoice.amount_net_pay
            invoice.withholding_tax_amount = withholding_tax_amount
            for line in invoice.payment_move_line_ids:
                if not line.withholding_tax_generated_by_move_id:
                    amount_net_pay_residual -= (line.debit or line.credit)
            invoice.amount_net_pay_residual = amount_net_pay_residual

    withholding_tax = fields.Boolean('Withholding Tax')
    withholding_tax_in_print = fields.Boolean(
        "Show Withholding Tax In Print", default=True)
    withholding_tax_line_ids = fields.One2many(
        'account.invoice.withholding.tax', 'invoice_id',
        'Withholding Tax Lines', copy=True,
        readonly=True, states={'draft': [('readonly', False)]})
    withholding_tax_amount = fields.Float(
        compute='_amount_withholding_tax',
        digits=dp.get_precision('Account'), string='Withholding tax Amount',
        store=True, readonly=True)
    amount_net_pay = fields.Float(
        compute='_amount_withholding_tax',
        digits=dp.get_precision('Account'), string='Net To Pay',
        store=True, readonly=True)
    amount_net_pay_residual = fields.Float(
        compute='_amount_withholding_tax',
        digits=dp.get_precision('Account'), string='Residual Net To Pay',
        store=True, readonly=True)

    @api.model
    def create(self, vals):
        invoice = super(AccountInvoice,
                        self.with_context(mail_create_nolog=True)).create(vals)

        if any(line.invoice_line_tax_wt_ids for line in
               invoice.invoice_line_ids) \
                and not invoice.withholding_tax_line_ids:
            invoice.compute_taxes()

        return invoice

    @api.onchange('invoice_line_ids')
    def _onchange_invoice_line_wt_ids(self):
        self.ensure_one()
        wt_taxes_grouped = self.get_wt_taxes_values()
        wt_tax_lines = [(5, 0, 0)]
        for tax in wt_taxes_grouped.values():
            wt_tax_lines.append((0, 0, tax))
        self.withholding_tax_line_ids = wt_tax_lines
        if len(wt_tax_lines) > 1:
            self.withholding_tax = True
        else:
            self.withholding_tax = False

    @api.multi
    def action_move_create(self):
        '''
        Split amount withholding tax on account move lines
        '''
        dp_obj = self.env['decimal.precision']
        res = super(AccountInvoice, self).action_move_create()

        for inv in self:
            # Rates
            rate_num = 0
            for move_line in inv.move_id.line_ids:
                if move_line.account_id.internal_type not in ['receivable',
                                                              'payable']:
                    continue
                rate_num += 1
            if rate_num:
                wt_rate = round(inv.withholding_tax_amount / rate_num,
                                dp_obj.precision_get('Account'))
            wt_residual = inv.withholding_tax_amount
            # Re-read move lines to assign the amounts of wt
            i = 0
            for move_line in inv.move_id.line_ids:
                if move_line.account_id.internal_type not in ['receivable',
                                                              'payable']:
                    continue
                i += 1
                if i == rate_num:
                    wt_amount = wt_residual
                else:
                    wt_amount = wt_rate
                wt_residual -= wt_amount
                # update line
                move_line.write({'withholding_tax_amount': wt_amount})
            # Create WT Statement
            self.create_wt_statement()
        return res

    @api.multi
    def get_wt_taxes_values(self):
        tax_grouped = {}
        for invoice in self:
            for line in invoice.invoice_line_ids:
                taxes = []
                for wt_tax in line.invoice_line_tax_wt_ids:
                    res = wt_tax.compute_tax(line.price_subtotal)
                    tax = {
                        'id': wt_tax.id,
                        'sequence': wt_tax.sequence,
                        'base': res['base'],
                        'tax': res['tax'],
                    }
                    taxes.append(tax)

                for tax in taxes:
                    val = {
                        'invoice_id': invoice.id,
                        'withholding_tax_id': tax['id'],
                        'tax': tax['tax'],
                        'base': tax['base'],
                        'sequence': tax['sequence'],
                    }

                    key = self.env['withholding.tax'].browse(
                        tax['id']).get_grouping_key(val)

                    if key not in tax_grouped:
                        tax_grouped[key] = val
                    else:
                        tax_grouped[key]['tax'] += val['tax']
                        tax_grouped[key]['base'] += val['base']
        return tax_grouped

    @api.one
    def create_wt_statement(self):
        """
        Create one statement for each withholding tax
        """
        wt_statement_obj = self.env['withholding.tax.statement']
        for inv_wt in self.withholding_tax_line_ids:
            wt_base_amount = inv_wt.base
            wt_tax_amount = inv_wt.tax
            if self.type in ['in_refund', 'out_refund']:
                wt_base_amount = -1 * wt_base_amount
                wt_tax_amount = -1 * wt_tax_amount
            val = {
                'wt_type': '',
                'date': self.move_id.date,
                'move_id': self.move_id.id,
                'invoice_id': self.id,
                'partner_id': self.partner_id.id,
                'withholding_tax_id': inv_wt.withholding_tax_id.id,
                'base': wt_base_amount,
                'tax': wt_tax_amount,
            }
            wt_statement_obj.create(val)

    @api.model
    def _get_payments_vals(self):
        payment_vals = super(AccountInvoice, self)._get_payments_vals()
        if self.payment_move_line_ids:
            for payment_val in payment_vals:
                move_line = self.env['account.move.line'].browse(
                    payment_val['payment_id'])
                if move_line.withholding_tax_generated_by_move_id:
                    payment_val['wt_move_line'] = True
                else:
                    payment_val['wt_move_line'] = False
        return payment_vals

    @api.model
    def _refund_cleanup_lines(self, lines):
        lines_values = super()._refund_cleanup_lines(lines)
        # Add same Withholding Taxes to Refund lines
        empty_wt_tax = self.env['withholding.tax'].browse()
        for line_index, line in enumerate(lines):
            # `line` can be a tax line
            # that does not have field `invoice_line_tax_wt_ids`
            line_wts = getattr(line, 'invoice_line_tax_wt_ids', empty_wt_tax)
            if line_wts:
                # There is no field to match the line and its values,
                # we have to trust that the order is preserved
                line_values = lines_values[line_index]
                line_values = line_values[2]  # values have format (0, 0, <values>)
                update_values = line._convert_to_write({
                    'invoice_line_tax_wt_ids': line_wts,
                })
                line_values.update(update_values)
        return lines_values

    def refund(self, date_invoice=None, date=None, description=None, journal_id=None):
        refunds = super().refund(
            date_invoice=date_invoice, date=date,
            description=description, journal_id=journal_id,
        )
        for refund in refunds:
            refund._onchange_invoice_line_wt_ids()
        return refunds


class AccountInvoiceLine(models.Model):
    _inherit = "account.invoice.line"

    @api.model
    def _default_withholding_tax(self):
        result = []
        fiscal_position_id = self._context.get('fiscal_position_id', False)
        if fiscal_position_id:
            fp = self.env['account.fiscal.position'].browse(fiscal_position_id)
            wt_ids = fp.withholding_tax_ids.mapped('id')
            result.append((6, 0, wt_ids))
        return result

    invoice_line_tax_wt_ids = fields.Many2many(
        comodel_name='withholding.tax', relation='account_invoice_line_tax_wt',
        column1='invoice_line_id', column2='withholding_tax_id', string='W.T.',
        default=_default_withholding_tax,
    )


class AccountInvoiceWithholdingTax(models.Model):
    '''
    Withholding tax lines in the invoice
    '''

    _name = 'account.invoice.withholding.tax'
    _description = 'Invoice Withholding Tax Line'

    def _prepare_price_unit(self, line):
        price_unit = 0
        price_unit = line.price_unit * \
            (1 - (line.discount or 0.0) / 100.0)
        return price_unit

    @api.depends('base', 'tax', 'invoice_id.amount_untaxed')
    def _compute_coeff(self):
        for inv_wt in self:
            if inv_wt.invoice_id.amount_untaxed:
                inv_wt.base_coeff = \
                    inv_wt.base / inv_wt.invoice_id.amount_untaxed
            if inv_wt.base:
                inv_wt.tax_coeff = inv_wt.tax / inv_wt.base

    invoice_id = fields.Many2one('account.invoice', string='Invoice',
                                 ondelete="cascade")
    withholding_tax_id = fields.Many2one('withholding.tax',
                                         string='Withholding tax',
                                         ondelete='restrict')
    sequence = fields.Integer('Sequence')
    base = fields.Float('Base')
    tax = fields.Float('Tax')
    base_coeff = fields.Float(
        'Base Coeff', compute='_compute_coeff', store=True, help="Coeff used\
         to compute amount competence in the riconciliation")
    tax_coeff = fields.Float(
        'Tax Coeff', compute='_compute_coeff', store=True, help="Coeff used\
         to compute amount competence in the riconciliation")
