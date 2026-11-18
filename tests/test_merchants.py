"""Which payments a merchant limit counts (merchants.py). Pure functions: no database, no network."""
import unicodedata
import unittest
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import merchants

WEEK_START = datetime(2026, 10, 18, 18, 30, tzinfo=timezone.utc)  # Mon 19 Oct 2026, 00:00 IST
MONTH_START = datetime(2026, 9, 30, 18, 30, tzinfo=timezone.utc)  # Thu 1 Oct 2026, 00:00 IST
WINDOWS = {
    "weekly": (WEEK_START, WEEK_START + timedelta(days=7)),
    "monthly": (MONTH_START, datetime(2026, 10, 31, 18, 30, tzinfo=timezone.utc)),
}


class MatchingTests(unittest.TestCase):
    def test_counted(self):
        for limit, name in [
            ("Swiggy", "Swiggy"),
            ("Swiggy", "Swiggy Ltd"),
            ("Amazon Pay", "Amazon Pay India"),
            ("Cult.fit", "Cult.fit Gym"),
        ]:
            with self.subTest(limit=limit, name=name):
                self.assertTrue(merchants.matches(limit, name))

    def test_documented_misses(self):
        for limit, name in [
            ("Swiggy", "SWIGGY"),  # different case
            ("Swiggy", "swiggy ltd"),
            ("Swiggy", "Swiggyone"),  # not a whole word
            ("Ola", "Kolar Stores"),
            ("Amazon", "Amazonpayindiap"),  # run together
            ("Cult Fit", "Cult.fit"),  # "fit" and "Fit" differ
            ("Cult.fit", "CULTFIT HEALTHIFYME"),
            ("Amazon Pay", "Amazon"),  # every word of the limit must be there
        ]:
            with self.subTest(limit=limit, name=name):
                self.assertFalse(merchants.matches(limit, name))

    def test_words_must_be_next_to_each_other_and_in_order(self):
        self.assertTrue(merchants.matches("Pay India", "Amazon Pay India"))
        self.assertFalse(merchants.matches("Amazon India", "Amazon Pay India"))
        self.assertFalse(merchants.matches("Pay Amazon", "Amazon Pay India"))

    def test_names_with_no_words_never_match_or_count(self):
        for empty in ["Unknown", "UNKNOWN", "  unknown  ", None, "", "   ", "!!!"]:
            with self.subTest(name=empty):
                self.assertEqual(merchants.words(empty), [])
                self.assertFalse(merchants.matches(empty, empty))
                self.assertFalse(merchants.matches(empty, "Swiggy"))
                self.assertFalse(merchants.matches("Swiggy", empty))
        # "Unknown" is only the placeholder when it's the whole name.
        self.assertEqual(merchants.words("Unknown Traders"), ["Unknown", "Traders"])

    def test_words_split_on_anything_but_letters_and_digits(self):
        self.assertEqual(merchants.words("Swiggy Ltd"), ["Swiggy", "Ltd"])
        self.assertEqual(merchants.words("Cult.fit"), ["Cult", "fit"])
        self.assertEqual(merchants.words("D-Mart/7-Eleven & Co."), ["D", "Mart", "7", "Eleven", "Co"])
        # Letters in any script are kept.
        self.assertEqual(merchants.words("Café Coffee Day"), ["Café", "Coffee", "Day"])
        self.assertEqual(merchants.words("Кафе Пушкин"), ["Кафе", "Пушкин"])
        self.assertTrue(merchants.matches("Café", "Café Noir"))
        self.assertFalse(merchants.matches("Café", "Cafe Noir"))

    def test_combining_marks_are_part_of_the_word(self):
        # Devanagari vowel signs and the virama are combining marks (Mn, Mc), not separators.
        self.assertEqual(merchants.words("स्विगी"), ["स्विगी"])
        self.assertEqual(merchants.words("गीता स्टोर"), ["गीता", "स्टोर"])
        self.assertEqual(merchants.words("ओला"), ["ओला"])
        self.assertFalse(merchants.matches("ओला", "ओली"))  # a different vowel sign: a different word
        self.assertFalse(merchants.matches("गो", "गीता स्टोर"))
        self.assertFalse(merchants.matches("स्विगी", "सि वा गु"))
        self.assertTrue(merchants.matches("स्विगी", "स्विगी इंस्टामार्ट"))
        # A mark on its own, with no letter to belong to, isn't a word.
        self.assertEqual(merchants.words("\u0301 !"), [])

    def test_decomposed_and_precomposed_names_are_the_same(self):
        nfc, nfd = "Café", unicodedata.normalize("NFD", "Café")
        self.assertNotEqual(nfc, nfd)
        self.assertEqual(merchants.words(nfd), merchants.words(nfc))
        self.assertEqual(merchants.words(nfd), ["Café"])
        self.assertEqual(merchants.clean(nfd), nfc)
        self.assertEqual(merchants.name_hash(merchants.clean(nfd)), merchants.name_hash(merchants.clean(nfc)))
        self.assertEqual(merchants.name_hash(nfd), merchants.name_hash(nfc))
        self.assertTrue(merchants.matches(nfc, unicodedata.normalize("NFD", "Café Noir")))
        self.assertTrue(merchants.matches(nfd, "Café Noir"))
        self.assertFalse(merchants.matches(nfd, "Cafe Noir"))

    def test_invalid_characters(self):
        for name in ["Swiggy \ud800", "Swi\x00ggy", "Swiggy\x07", "Swiggy\x7f", "Swiggy\x9f", "Swiggy \ufffd"]:
            with self.subTest(name=name.encode("unicode_escape")):
                self.assertTrue(merchants.has_invalid_characters(name))
                marked = merchants.mark_invalid_characters(name)
                marked.encode("utf-8")  # always encodable
                self.assertTrue(merchants.has_invalid_characters(marked))
        for name in ["Swiggy Ltd", " Swiggy\tLtd\n", "स्विगी", "Swiggy\u200dLtd"]:  # a zero-width joiner is format, not control
            with self.subTest(name=name.encode("unicode_escape")):
                self.assertFalse(merchants.has_invalid_characters(name))
                self.assertEqual(merchants.mark_invalid_characters(name), name)

    def test_clean(self):
        self.assertEqual(merchants.clean("  Swiggy   Ltd "), "Swiggy Ltd")
        self.assertEqual(merchants.clean("Swiggy\tLtd"), "Swiggy Ltd")
        self.assertEqual(merchants.clean("SWIGGY"), "SWIGGY")  # case is kept

    def test_name_hash_keeps_case(self):
        self.assertEqual(len(merchants.name_hash("Swiggy")), 64)
        self.assertEqual(merchants.name_hash("Swiggy"), merchants.name_hash("Swiggy"))
        self.assertNotEqual(merchants.name_hash("Swiggy"), merchants.name_hash("SWIGGY"))


class KnownNamesTests(unittest.TestCase):
    def test_distinct_with_case_counted_and_sorted(self):
        rows = [("Swiggy",), ("SWIGGY",), ("Swiggy",), ("Swiggy Ltd",), ("Unknown",), (None,), ("!!!",), ("Zomato",)]
        typed = ["Cult.fit", "Zomato", "Blinkit", "unknown"]
        self.assertEqual(merchants.known_names(rows, typed), [
            ("Swiggy", 2),
            ("SWIGGY", 1),  # upper case sorts before lower case: the order is the same on every run
            ("Swiggy Ltd", 1),
            ("Zomato", 1),  # typed and paid: listed once, with its payments
            ("Blinkit", 0),
            ("Cult.fit", 0),
        ])

    def test_names_that_are_the_same_once_cleaned_are_one(self):
        nfd = unicodedata.normalize("NFD", "Café")
        rows = [("Amazon  Pay",), ("Amazon Pay",), (" Amazon\tPay",), ("Café",), (nfd,)]
        self.assertEqual(merchants.known_names(rows, ["Amazon Pay", "Zepto  Now"]), [
            ("Amazon Pay", 3),
            ("Café", 2),
            ("Zepto Now", 0),
        ])

    def test_takes_full_payment_rows(self):
        rows = [("Swiggy", Decimal("10"), WEEK_START)] * 2
        self.assertEqual(merchants.known_names(rows, []), [("Swiggy", 2)])


class MeterTests(unittest.TestCase):
    def test_sums_matching_rows_in_each_window(self):
        rows = [
            ("Swiggy", Decimal("100.10"), WEEK_START),  # exactly at the week's start: counts
            ("Swiggy Ltd", Decimal("200.20"), WEEK_START + timedelta(days=7)),  # at its end: not this week
            ("Swiggy", Decimal("50"), MONTH_START),  # first moment of the month
            ("Swiggy", Decimal("75"), MONTH_START - timedelta(seconds=1)),  # last month: only in the names
            ("SWIGGY", Decimal("999"), WEEK_START),  # different case
            ("Zomato", Decimal("999"), WEEK_START),
        ]
        weekly, monthly, counts = merchants.meter("Swiggy", rows, WINDOWS)
        self.assertEqual((weekly, monthly), (Decimal("100.10"), Decimal("350.30")))
        self.assertEqual(counts, ["Swiggy", "Swiggy Ltd"])

    def test_names_are_ordered_by_payments_then_name(self):
        rows = [("Swiggy Ltd", 1, WEEK_START)] * 3 + [("Swiggy", 1, WEEK_START)] + [("Swiggy Instamart", 1, WEEK_START)]
        self.assertEqual(merchants.meter("Swiggy", rows, WINDOWS)[2], ["Swiggy Ltd", "Swiggy", "Swiggy Instamart"])

    def test_counted_names_are_cleaned(self):
        rows = [("Swiggy  Ltd", 1, WEEK_START), ("Swiggy Ltd", 2, WEEK_START), ("Swiggy", 3, WEEK_START)]
        self.assertEqual(merchants.meter("Swiggy", rows, WINDOWS), (Decimal("6.00"), Decimal("6.00"), ["Swiggy Ltd", "Swiggy"]))

    def test_sums_are_rounded_to_paise(self):
        rows = [("Swiggy", 0.1, WEEK_START), ("Swiggy", 0.2, WEEK_START)]
        self.assertEqual(merchants.meter("Swiggy", rows, WINDOWS)[:2], (Decimal("0.30"), Decimal("0.30")))

    def test_nothing_matched(self):
        self.assertEqual(merchants.meter("Swiggy", [("Zomato", 5, WEEK_START)], WINDOWS), (Decimal("0.00"), Decimal("0.00"), []))
        self.assertEqual(merchants.meter("Unknown", [("Unknown", 5, WEEK_START)], WINDOWS)[2], [])



class ShareWordTests(unittest.TestCase):
    """The loose rule for matching a notice or a refund to a payment: one shared meaningful word, any case."""
    def assertShare(self, pairs, expected):
        for a, b in pairs:
            with self.subTest(a=a, b=b):
                self.assertEqual(merchants.share_word(a, b), expected)
                self.assertEqual(merchants.share_word(b, a), expected)

    def test_shared_words(self):
        self.assertShare([
            ("NETFLIX", "Netflix Upi"),
            ("Netflix Entertainment Services", "Netflix.com"),
            ("Netflix", "Rs 649.00 debited via UPI AutoPay from A/c XX1234 to NETFLIX"),
            ("Café Coffee Day", "CAFÉ"),
            ("स्विगी", "स्विगी इंस्टामार्ट"),
        ], True)

    def test_common_words_never_match(self):
        self.assertShare([
            ("HDFC Bank", "HDFC Bank Credit Card"),
            ("HDFC Bank", "Rs 649.00 debited from HDFC Bank A/c XX1234"),
            ("Amazon.com", "Netflix.com"),
            ("Unknown", "Unknown Rs 500 debited"),
            ("Netflix", "Added by hand in Folio"),
        ], False)

    def test_whole_words_of_three_or_more_with_a_letter(self):
        self.assertShare([("Ola", "Kolar Stores"), ("Vi", "Vi Recharge"), ("7001", "Card 7001"), ("Netflix", "Spotify")], False)

    def test_missing_text(self):
        for a, b in [(None, "Netflix"), ("Netflix", None), ("", ""), (None, None)]:
            with self.subTest(a=a, b=b):
                self.assertFalse(merchants.share_word(a, b))

    def test_common_words_are_casefolded(self):
        for word in merchants.COMMON_WORDS:
            with self.subTest(word=word):
                self.assertEqual(word, word.casefold())
                self.assertNotIn(" ", word)
        for word in ("hdfc", "autopay", "folio", "com"):
            self.assertIn(word, merchants.COMMON_WORDS)


if __name__ == "__main__":
    unittest.main()
