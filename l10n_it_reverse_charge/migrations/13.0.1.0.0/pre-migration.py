#  Copyright 2021 Simone Vanin - Agile Business Group
#  License AGPL-3.0 or later (http://www.gnu.org/licenses/agpl.html).

from openupgradelib import openupgrade


@openupgrade.migrate()
def migrate(env, version):
    if not version:
        return
    cr = env.cr

    # list of account_move fields for further insert
    openupgrade.logged_query(
        cr,
        """
        SELECT STRING_AGG(column_name, ', ')
        FROM information_schema.columns
        WHERE table_name = 'account_move'
            AND table_schema = 'public'
            AND column_name NOT IN('id')
        ;
        """,
    )

    move_fields = "".join(cr.fetchone())
    query_move = (
        "select "
        + move_fields.replace("create_date", "NOW()").replace("write_date", "NOW()")
        + " from account_move "
    )

    # List of rc invoices
    openupgrade.logged_query(
        cr,
    """
            SELECT
                am.company_id,
                full_reconcile_id,
                am.id,
                inv.rc_purchase_invoice_id,
                aml.account_id
            FROM
                account_move_line AS aml
            JOIN
                account_move AS am
                ON am.id = aml.move_id
            JOIN
                account_invoice AS inv
                ON inv.move_id = am.id
            JOIN
                account_full_reconcile AS afr
                ON afr.id = aml.full_reconcile_id
            WHERE
                inv.rc_purchase_invoice_id IS NOT NULL
                AND aml.move_id NOT IN (
                    SELECT
                        ap.move_id
                    FROM
                        account_payment AS ap
                );
        """,
    )

    res = cr.fetchall()

    for r in res:
        company_id, fr_id, rc_inv, supp_inv, rc_dest_acc_id = r
        openupgrade.logged_query(
            cr,
            """
                SELECT
                    am.currency_id,
                    am.partner_id,
                    am.create_uid,
                    aml.account_id
                FROM
                    account_move AS am
                JOIN
                    account_move_line AS aml
                    ON am.id = aml.move_id
                WHERE
                    am.old_invoice_id = {supp_inv}
                    AND aml.account_id IN (
                        SELECT
                            aa.id
                        FROM
                            account_account AS aa
                        WHERE
                            aa.internal_type = 'payable'
                    );
            """.format(
                supp_inv=supp_inv
            ),
        )
        payment_result = cr.fetchall()
        if payment_result:
            payment_vals = payment_result[0]
            currency_id = payment_vals[0]
            partner_id = payment_vals[1]
            create_uid = payment_vals[2]
            supp_dest_acc_id = payment_vals[3]

            openupgrade.logged_query(
                cr,
                """
                    SELECT
                        move_id,
                        full_reconcile_id,
                        id,
                        ABS(amount_currency)
                    FROM
                        account_move_line
                    WHERE
                        move_id IN (
                            SELECT
                                move_id
                            FROM
                                account_move_line
                            WHERE
                                full_reconcile_id = {fr_id}
                                AND journal_id IN (
                                    SELECT
                                        payment_journal_id
                                    FROM
                                        account_rc_type
                                    WHERE
                                        method = 'selfinvoice'
                                )
                        )
                    ORDER BY
                        full_reconcile_id;
              """.format(
                    fr_id=fr_id
                ),
            )

            # split payment move in two moves
            move_vals = {}
            # format [(inv_move, payment_move)]
            move_ids = []
            supp_amount = 0
            rc_amount = 0
            for move_id, full_reconcile_id, move_line_id, currency_amount in cr.fetchall():
                if full_reconcile_id == fr_id:
                    full_reconcile_id = rc_inv
                    rc_amount = currency_amount
                else:
                    if rc_amount and rc_amount == currency_amount:
                        full_reconcile_id = rc_inv
                    else:
                        full_reconcile_id = supp_inv
                        supp_amount = currency_amount
                if not move_ids:
                    move_ids = [(full_reconcile_id, move_id)]
                move_vals[full_reconcile_id] = [move_line_id] if full_reconcile_id not in move_vals else move_vals[full_reconcile_id] + [move_line_id]

            # clone payment move and append to list
            openupgrade.logged_query(
                cr,
                """
                INSERT INTO account_move ({move_fields})
                {query_move}
                WHERE id = {move_id}
                RETURNING id;
                """.format(
                    move_fields=move_fields,
                    query_move=query_move,
                    move_id=move_ids[0][1],
                ),
            )
            move_ids.append(
                (supp_inv if move_ids[0] == rc_inv else rc_inv, cr.fetchone()[0])
            )

            # create an account_payment record for every payment move
            # update move lines with new move id
            for inv, move_id in move_ids:
                openupgrade.logged_query(
                    cr,
                    """
                    INSERT INTO account_payment (
                        move_id,
                        is_reconciled,
                        is_matched,
                        is_internal_transfer,
                        payment_method_id,
                        amount,
                        payment_type,
                        partner_type,
                        currency_id,
                        partner_id,
                        destination_account_id,
                        create_uid,
                        create_date,
                        write_uid,
                        write_date
                    )
                    VALUES (
                        {move_id},
                        't',
                        't',
                        'f',
                        {method},
                        {amount},
                        {payment_type},
                        {partner_type},
                        {currency_id},
                        {partner_id},
                        {dest_acc_id},
                        {create_uid},
                        NOW(),
                        {write_uid},
                        NOW()
                    )
                    RETURNING id;
                    """.format(
                        move_id=move_id,
                        method=1 if inv == supp_inv else 2,
                        amount=supp_amount if inv == supp_inv else rc_amount,
                        payment_type="'outbound'" if inv == supp_inv else "'inbound'",
                        partner_type="'supplier'" if inv == supp_inv else "'customer'",
                        currency_id=currency_id,
                        partner_id=partner_id,
                        dest_acc_id=supp_dest_acc_id if inv == supp_inv else rc_dest_acc_id,
                        create_uid=create_uid,
                        write_uid=create_uid,
                    ),
                )
                payment_id = cr.fetchone()[0]

                line_ids = ",".join([str(line_id) for line_id in move_vals[inv]])
                openupgrade.logged_query(
                    cr,
                    """
                    UPDATE account_move_line
                    SET
                        move_id = {move_id},
                        payment_id = {payment_id}
                    WHERE
                        id IN ({line_ids});
                    """.format(
                        move_id=move_id, payment_id=payment_id, line_ids=line_ids
                    ),
                )

                openupgrade.logged_query(
                    cr,
                    """
                    UPDATE account_move
                    SET
                        payment_id = {payment_id}
                    WHERE
                        id = {move_id};
                    """.format(
                        payment_id=payment_id, move_id=move_id
                    ),
                )

    # pre-populate columns
    if openupgrade.table_exists(env.cr, "account_invoice"):
        for table, column, column_type in [
            ("account_move", "rc_self_invoice_id", "integer"),
            ("account_move", "rc_purchase_invoice_id", "integer"),
            ("account_move", "rc_self_purchase_invoice_id", "integer"),
            ("account_move_line", "rc", "boolean"),
        ]:
            if not openupgrade.column_exists(env.cr, table, column):
                openupgrade.add_fields(
                    env,
                    [
                        (
                            column,
                            ".".join(table.split("_")),
                            False,
                            column_type,
                            False,
                            "l10n_it_reverse_charge",
                        )
                    ],
                )
        # copy columns
        openupgrade.logged_query(
            cr,
            """
                UPDATE account_move
                SET
                    rc_self_invoice_id = invsi.move_id
                FROM
                    account_invoice inv
                    JOIN account_invoice invsi ON invsi.id = inv.rc_self_invoice_id
                WHERE
                    account_move.id = inv.move_id;
          """,
        )
        openupgrade.logged_query(
            cr,
            """
                UPDATE account_move
                SET
                    rc_purchase_invoice_id = invpi.move_id
                FROM
                    account_invoice inv
                    JOIN account_invoice invpi ON invpi.id = inv.rc_purchase_invoice_id
                WHERE
                    account_move.id = inv.move_id;
            """,
        )
        openupgrade.logged_query(
            cr,
            """
                UPDATE account_move
                SET
                    rc_self_purchase_invoice_id = invspi.move_id
                FROM
                    account_invoice inv
                    JOIN account_invoice invspi ON invspi.id = inv.rc_self_purchase_invoice_id
                WHERE
                    account_move.id = inv.move_id;
            """,
        )

        openupgrade.logged_query(
            cr,
            """
                UPDATE
                    account_move_line aml
                SET
                    rc = invl.rc
                FROM
                    account_invoice_line invl
                JOIN
                    account_invoice inv ON inv.id = invl.invoice_id
                WHERE
                    aml.move_id = inv.move_id;
            """,
        )
