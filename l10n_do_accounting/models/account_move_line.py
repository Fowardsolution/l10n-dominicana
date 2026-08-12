import logging

from odoo import models, fields, api, _
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)


class AccountMoveLine(models.Model):
    _inherit = "account.move.line"

    l10n_do_itbis_amount = fields.Monetary(
        string="ITBIS Amount",
        store=True,
        readonly=True,
        currency_field="currency_id",
        compute="_compute_totals",
    )

    @api.model
    def _l10n_do_find_tax_group(self, company, code):
        """Return the dominican tax group ``code`` of ``company``, if any.

        Since v17 the chart template engine names these records
        ``account.<company_id>_tax_group_<code>``, but databases migrated from
        an earlier version may still carry the old ``l10n_do.tax_group_<code>``
        ones. Look the record up under both conventions and return an empty
        recordset when it is nowhere to be found, so that a company without the
        dominican chart of accounts cannot break invoicing with a bare
        ValueError.
        """
        group = self.env.ref(
            "account.%s_tax_group_%s" % (company.id, code),
            raise_if_not_found=False,
        )
        if not group:
            # Only trust the pre-v17 record when it does belong to `company`,
            # otherwise we would silently match another company's taxes.
            legacy_group = self.env.ref(
                "l10n_do.tax_group_%s" % code, raise_if_not_found=False
            )
            if legacy_group and legacy_group.company_id in (
                company,
                self.env["res.company"],  # company-less, pre-v17 record
            ):
                group = legacy_group
        return group or self.env["account.tax.group"]

    @api.depends("quantity", "discount", "price_unit", "tax_ids", "currency_id")
    def _compute_totals(self):
        super(AccountMoveLine, self)._compute_totals()
        for line in self:
            if line.display_type != "product" or not line.move_id.is_ecf_invoice:
                line.l10n_do_itbis_amount = False
                continue

            group_itbis = self._l10n_do_find_tax_group(line.company_id, "itbis")
            if not group_itbis:
                # Never raise here: this compute runs while the move is being
                # created, so it would abort invoicing altogether.
                _logger.warning(
                    "No ITBIS tax group found for company %s (%s); "
                    "l10n_do_itbis_amount left empty on an e-CF invoice line.",
                    line.company_id.display_name,
                    line.company_id.id,
                )
                line.l10n_do_itbis_amount = False
                continue

            line_itbis_taxes = line.tax_ids.filtered(
                lambda t: t.tax_group_id == group_itbis
            )
            price_unit = line.price_unit
            if line.discount:
                price_unit = price_unit - (price_unit * (line.discount / 100))
            itbis_taxes_data = line_itbis_taxes.compute_all(
                price_unit=price_unit,
                quantity=line.quantity,
            )
            line.l10n_do_itbis_amount = sum(
                [t["amount"] for t in itbis_taxes_data["taxes"]]
            )

    def _get_l10n_do_line_amounts(self):
        group_itbis = self._l10n_do_find_tax_group(self.company_id, "itbis")
        group_isr = self._l10n_do_find_tax_group(self.company_id, "isr")
        missing_groups = [
            name
            for name, group in (("ITBIS", group_itbis), ("ISR", group_isr))
            if not group
        ]
        if missing_groups:
            # Unlike the compute above this runs on reports and on electronic
            # invoicing, where reporting zeroed amounts to the DGII is worse
            # than refusing to go on.
            raise UserError(
                _(
                    "The %(groups)s tax group(s) could not be found for company "
                    "%(company)s. Install the dominican chart of accounts on "
                    "this company before issuing fiscal documents."
                )
                % {
                    "groups": ", ".join(missing_groups),
                    "company": self.company_id.display_name,
                }
            )

        tax_lines = self.filtered(
            lambda x: x.tax_group_id.id
            in [
                group_itbis.id,
                group_isr.id,
            ]
        )
        itbis_tax_lines = tax_lines.filtered(
            lambda line: line.tax_group_id == group_itbis
        )
        isr_tax_lines = tax_lines.filtered(lambda line: line.tax_group_id == group_isr)

        invoice_line_ids = self.filtered(lambda x: x.display_type == "product")
        taxed_lines = invoice_line_ids.filtered(
            lambda x: x.tax_ids and any(tax for tax in x.tax_ids if tax.amount)
        )
        exempt_lines = invoice_line_ids.filtered(
            lambda x: not x.tax_ids or any(tax for tax in x.tax_ids if not tax.amount)
        )
        itbis_taxed_lines = taxed_lines.filtered(
            lambda line: group_itbis in line.tax_ids.mapped("tax_group_id")
        )
        isr_taxed_lines = taxed_lines.filtered(
            lambda line: group_isr in line.tax_ids.mapped("tax_group_id")
        )

        itbis_tax_amount_map = {
            "18": (18, 1.8),
            "16": (16, 1.6),
        }

        result = {
            "base_amount": sum(taxed_lines.mapped("price_subtotal")),
            "exempt_amount": sum(exempt_lines.mapped("price_subtotal")),
            "itbis_18_tax_amount": sum(
                self.currency_id.round(line.amount_currency)
                for line in itbis_tax_lines.filtered(
                    lambda tl: tl.tax_line_id.amount in itbis_tax_amount_map["18"]
                )
            ),
            "itbis_18_base_amount": sum(
                itbis_taxed_lines.filtered(
                    lambda line: any(
                        tax
                        for tax in line.tax_ids
                        if tax.amount in itbis_tax_amount_map["18"]
                    )
                ).mapped("amount_currency")
            ),
            "itbis_16_tax_amount": sum(
                self.currency_id.round(line.amount_currency)
                for line in itbis_tax_lines.filtered(
                    lambda tl: tl.tax_line_id.amount in itbis_tax_amount_map["16"]
                )
            ),
            "itbis_16_base_amount": sum(
                itbis_taxed_lines.filtered(
                    lambda line: any(
                        tax
                        for tax in line.tax_ids
                        if tax.amount in itbis_tax_amount_map["16"]
                    )
                ).mapped("amount_currency")
            ),
            "itbis_0_tax_amount": 0,  # not supported
            "itbis_0_base_amount": 0,  # not supported
            "itbis_withholding_amount": sum(
                self.currency_id.round(line.amount_currency)
                for line in itbis_tax_lines.filtered(
                    lambda tl: tl.tax_line_id.amount < 0
                )
            ),
            "itbis_withholding_base_amount": sum(
                itbis_taxed_lines.filtered(
                    lambda line: any(tax for tax in line.tax_ids if tax.amount < 0)
                ).mapped("amount_currency")
            ),
            "isr_withholding_amount": sum(
                self.currency_id.round(line.amount_currency)
                for line in isr_tax_lines.filtered(lambda tl: tl.tax_line_id.amount < 0)
            ),
            "isr_withholding_base_amount": sum(
                isr_taxed_lines.filtered(
                    lambda line: any(tax for tax in line.tax_ids if tax.amount < 0)
                ).mapped("amount_currency")
            ),
        }

        # convert values to positives
        for key, value in result.items():
            result[key] = abs(value)

        result["l10n_do_invoice_total"] = (
            self.move_id.amount_untaxed
            + result["itbis_18_tax_amount"]
            + result["itbis_16_tax_amount"]
            + result["itbis_0_tax_amount"]
        )

        if self.currency_id != self.company_id.currency_id:
            rate = (self.currency_id + self.company_id.currency_id)._get_rates(
                self.company_id, self.move_id.date
            ).get(self.currency_id.id) or 1
            currency_vals = {}
            for k, v in result.items():
                currency_vals[k + "_currency"] = v / rate
            result.update(currency_vals)

        return result
