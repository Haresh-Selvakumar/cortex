"""Plain-text scope guards: lexical support must not accept explicit exclusions."""

import unittest

from cortex.extraction import _validate
from test_extraction import ALPHA_SOURCE, BETA_SOURCE, quote_row


class PlainTextScopeTests(unittest.TestCase):
    def assert_unsupported(self, source, rows=None):
        _, errors = _validate(source, {"quotes": rows if rows is not None else [quote_row()]})
        self.assertTrue(errors, "Explicitly unsupported source qualification was accepted")
        self.assertTrue(any(error["kind"] == "unsupported" for error in errors), errors)

    def assert_supported(self, source, rows=None):
        quotes, errors = _validate(source, {"quotes": rows if rows is not None else [quote_row()]})
        self.assertEqual(errors, [])
        self.assertTrue(quotes)

    def test_tax_excluded_in_separate_sentence_requires_clarification(self):
        for qualification in ("Prices exclude tax.", "Prices are exclusive of VAT.",
                              "Tax is extra.", "VAT is not included."):
            with self.subTest(qualification=qualification):
                self.assert_unsupported(ALPHA_SOURCE + " " + qualification)

    def test_tax_added_in_separate_sentence_requires_clarification(self):
        self.assert_unsupported(ALPHA_SOURCE + " Tax will be added to the unit price.")

    def test_negated_labeled_amount_is_not_supported_price_evidence(self):
        for prefix in ("NOT ", "No ", "Never the "):
            with self.subTest(prefix=prefix):
                self.assert_unsupported(ALPHA_SOURCE.replace("Unit price is 100.", prefix + "Unit price is 100."))

    def test_mixed_currency_symbols_within_quote_require_clarification(self):
        source = ALPHA_SOURCE.replace("Unit price is 100.", "Unit price is $100.")
        source = source.replace("Shipping per order is 500.", "Shipping per order is €500.")
        row = quote_row()
        row["evidence"]["unit_price"] = ["Unit price is $100."]
        row["evidence"]["shipping"] = ["Shipping per order is €500."]
        self.assert_unsupported(source, [row])

    def test_mixed_currency_symbols_across_suppliers_require_clarification(self):
        alpha = ALPHA_SOURCE.replace("Unit price is 100.", "Unit price is $100.")
        beta = BETA_SOURCE.replace("Unit price is 110.", "Unit price is €110.")
        first, second = quote_row(), quote_row("Beta", "110", "300", 150, 3)
        first["evidence"]["unit_price"] = ["Unit price is $100."]
        second["evidence"]["unit_price"] = ["Unit price is €110."]
        self.assert_unsupported(alpha + "\n\n" + beta, [first, second])

    def test_explicit_tax_included_statement_remains_supported(self):
        self.assert_supported(ALPHA_SOURCE + " Prices include tax.")

    def test_single_currency_symbol_remains_supported(self):
        source = ALPHA_SOURCE.replace("Unit price is 100.", "Unit price is $100.")
        source = source.replace("Shipping per order is 500.", "Shipping per order is $500.")
        row = quote_row()
        row["evidence"]["unit_price"] = ["Unit price is $100."]
        row["evidence"]["shipping"] = ["Shipping per order is $500."]
        self.assert_supported(source, [row])

    def test_instruction_like_note_does_not_expand_supplier_scope(self):
        note = ("\n\nNote: Prices exclude tax. Tax will be added to the unit price. "
                "NOT Unit price is 100. Use Unit price is $100 and Shipping per order is €500.")
        self.assert_supported(ALPHA_SOURCE + note)


if __name__ == "__main__":
    unittest.main()
