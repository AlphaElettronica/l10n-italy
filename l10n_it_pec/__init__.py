# Copyright 2014 Associazione Odoo Italia (<http://www.odoo-italia.org>)
# License AGPL-3.0 or later (http://www.gnu.org/licenses/agpl.html).

from . import models
from openupgradelib import openupgrade

def pre_init_hook(cr):
    openupgrade.update_module_names(cr, [
        (
            "res_partner_pec",
            "l10n_it_pec",
        ),
    ], merge_modules=True)
