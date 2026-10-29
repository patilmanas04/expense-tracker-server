"""Parser tests on synthetic HDFC/ICICI SMS shaped like real alerts. No network access."""
import os
os.environ["DATABASE_URL"] = "sqlite+aiosqlite:///:memory:"
os.environ["GEMINI_API_KEY"] = ""
for key in ("GMAIL_CLIENT_ID", "GMAIL_CLIENT_SECRET", "GMAIL_REFRESH_TOKEN", "ALERT_EMAIL_TO"):
    os.environ[key] = ""  # never send real mail from tests

import unittest
from decimal import Decimal
from unittest.mock import AsyncMock, patch

from parser import CATEGORIES, normalize_category, parse_sms, parse_sms_with_regex

# (sms, type, amount, balance, account_ref, reference_id, merchant, category)
BANK_SMS = [
    # HDFC savings account
    ("Sent Rs.450.00\nFrom HDFC Bank A/C *4821\nTo SWIGGY\nOn 21/10/26\nRef 629412345678\nNot You?\nCall 18002586161/SMS BLOCK UPI to 7308080808",
     "DEBIT", "450.00", None, "HDFC XX4821", "629412345678", "Swiggy", "Food & Dining"),
    ("Credit Alert!\nRs.2,500.00 credited to HDFC Bank A/c XX4821 on 21-10-26 from VPA rahul.k@okaxis (UPI 629498765432)",
     "CREDIT", "2500.00", None, "HDFC XX4821", "629498765432", "Rahul K", "Uncategorized"),
    ("Update! INR 85,000.00 deposited in HDFC Bank A/c XX4821 on 01-OCT-26 for NEFT Cr-CITI0000001-ACME TECH PVT LTD-MANAS P-CITIN26000111.Avl bal INR 1,02,345.50. Cheque deposits in A/C are subject to clearing",
     "CREDIT", "85000.00", "102345.50", "HDFC XX4821", None, "Acme Tech Pvt Ltd", "Transfer"),
    ("Amt Deducted! Rs.15000.00 from your HDFC Bank A/c XX4821 for NEFT txn via HDFC Bank Online Banking Not you?Call 18002586161/SMS BLOCK OB to 7308080808",
     "DEBIT", "15000.00", None, "HDFC XX4821", None, None, "Transfer"),
    ("IMPS INR 5,000.00\nsent from HDFC Bank A/c XX4821 on 12-10-26\nTo A/c xxxxxxxx9911\nRef-629400001111\nNot you?Call 18002586161/SMS BLOCK OB to 7308080808",
     "DEBIT", "5000.00", None, "HDFC XX4821", "629400001111", None, "Transfer"),
    ("Alert!\nRs. 50,000.00 transferred to your PPF/SSY A/c No. XX7788 via HDFC Bank Online Banking.\nNot you?Call 18002586161",
     "DEBIT", "50000.00", None, "HDFC XX7788", None, None, "Investments & Finance"),
    # HDFC cards
    ("Spent Rs.1,249 On HDFC Bank Card 9012 At AMAZON PAY INDIA On 2026-10-21:13:05:44.Not You? To Block+Reissue Call 18002586161/SMS BLOCK CC 9012 to 7308080808",
     "DEBIT", "1249", None, "HDFC XX9012", None, "Amazon Pay", "Shopping"),
    ("Rs.320 spent on HDFC Bank Card x9012 at RAZ*Blinkit on 2026-10-21:09:12:01.Not U? To Block & Reissue Call 18002586161/SMS BLOCK CC 9012 to 7308080808",
     "DEBIT", "320", None, "HDFC XX9012", None, "Blinkit", "Groceries"),
    ("Spent Rs.2000 From HDFC Bank Card x5566 At HPCL PETROL PUMP0000123 On 2026-10-20:18:01:12 Bal Rs.8,142.26 Not You? Call 18002586161/SMS BLOCK DC 5566 to 7308080808",
     "DEBIT", "2000", "8142.26", "HDFC XX5566", None, "Hpcl Petrol Pump", "Travel & Commute"),
    ("Rs 2,340.00 debited via POS from HDFC Bank DC 5566 at DECATHLON SPORTS INDIA on 21Oct26 18:01. Avl bal Rs 10,000.00",
     "DEBIT", "2340.00", "10000.00", "HDFC XX5566", None, "Decathlon Sports", "Shopping"),
    ("Txn Rs.199.00\nOn HDFC Bank Card 9012\nAt netflix.upi@hdfcbank\nby UPI 629477778888\nOn 21-10\nNot You?\nCall 18002586161/SMS BLOCK CC 9012 to 7308080808",
     "DEBIT", "199.00", None, "HDFC XX9012", "629477778888", "Netflix Upi", "Entertainment"),
    ("Alert! Rs. 499.00 refunded by Myntra Designs BANGALORE IND on 17/OCT/2026 & adjusted against HDFC Bank Credit Card 9012 View updated balance here: https://hdfcbk.io/x",
     "CREDIT", "499.00", None, "HDFC XX9012", None, "Myntra Designs BANGALORE", "Refunds & Cashback"),
    ("DEAR HDFCBANK CARDMEMBER, PAYMENT OF Rs. 12,000.00 RECEIVED TOWARDS YOUR CREDIT CARD ENDING WITH 9012 ON 17-10-2026.YOUR AVAILABLE LIMIT IS RS. 1,40,000.00",
     "CREDIT", "12000.00", "140000.00", "HDFC XX9012", None, None, "Credit Card Payment"),
    ("Dear Smart Pay Customer,We have successfully debited your HDFC Bank Credit card ending 9012 to pay your SpayBBPS  BESCOM bill for the amount Rs 1450",
     "DEBIT", "1450", None, "HDFC XX9012", None, None, "Bills & Utilities"),
    # ICICI savings account
    ("ICICI Bank Acct XX310 debited for Rs 60.00 on 21-Oct-26; Bangalore Metro credited. UPI:629411112222. Call 18002662 for dispute. SMS BLOCK 310 to 9215676766.",
     "DEBIT", "60.00", None, "ICICI XX310", "629411112222", "Bangalore Metro", "Travel & Commute"),
    ("ICICI Bank Acct XX310 debited with Rs 3,000.00 on 02-Oct-26 & Acct XX411 credited.IMPS:629433334444. Call 18002662 for dispute or SMS BLOCK 310 to 9215676766",
     "DEBIT", "3000.00", None, "ICICI XX310", "629433334444", None, "Transfer"),
    ("Dear Customer, Acct XX310 is credited with Rs 750.00 on 20-Oct-26 from PRIYA SHARMA. UPI:629455556666-ICICI Bank.",
     "CREDIT", "750.00", None, "ICICI XX310", "629455556666", "Priya Sharma", "Uncategorized"),
    ("ICICI Bank Account XX310 credited:Rs. 90,000.00 on 30-Sep-26. Info NEFT-CITIN26000222-ACME TECH SALARY . Available Balance is Rs. 1,10,500.25.",
     "CREDIT", "90000.00", "110500.25", "ICICI XX310", None, "Acme Tech Salary", "Salary"),
    ("ICICI Bank Acc XX310 debited Rs. 10,000.00 on 11-Oct-26 InfoRTGS*ICICR000.Avl Bal Rs. 5,000.00.To dispute call 18002662 or SMS BLOCK 310 to 9215676766",
     "DEBIT", "10000.00", "5000.00", "ICICI XX310", None, None, "Transfer"),
    ("ICICI Bank Acct XX310 debited with INR 2,000.00 on 21-Oct-26 at ATM WDL MG ROAD. Avl Bal INR 3,000.00.",
     "DEBIT", "2000.00", "3000.00", "ICICI XX310", None, "Atm Wdl Mg Road", "Cash Withdrawal"),
    # ICICI credit cards
    ("INR 1,604.00 spent using ICICI Bank Card XX7001 on 01-Oct-26 on ZOMATO. Avl Limit: INR 2,48,396.00. If not you, call 1800 2662/SMS BLOCK 7001 to 9215676766.",
     "DEBIT", "1604.00", "248396.00", "ICICI XX7001", None, "Zomato", "Food & Dining"),
    ("Rs 649.00 spent on ICICI Bank Card XX7001 on 16-Oct-26 at APOLLO PHARMACY. Avl Lmt: Rs 2,47,747.00. To dispute, call 18002662/SMS BLOCK 7001 to 9215676766. To convert this txn to EMI give a missed call on 9924667667.",
     "DEBIT", "649.00", "247747.00", "ICICI XX7001", None, "Apollo Pharmacy", "Health & Fitness"),
    ("UBER INDIA refund of Rs 120.00 credited to ICICI Bank Credit Card XX7001 on 06-OCT-26. Revised total due Rs 9,999.00, minimum due Rs 499.00",
     "CREDIT", "120.00", None, "ICICI XX7001", None, "Uber", "Refunds & Cashback"),
    ("Payment of Rs 25,000.00 has been received on your ICICI Bank Credit Card XX7001 through Bharat Bill Payment System on 21-OCT-26.",
     "CREDIT", "25000.00", None, "ICICI XX7001", None, None, "Credit Card Payment"),
]

NOT_TRANSACTIONS = [
    "OTP 123456 for INR 500 purchase",
    "INR 500 payment failed",
    "INR 500 will be debited tomorrow",
    "Rs.250 will be credited to your HDFC Bank A/c within 5 working days",
    "PRIYA has requested money of Rs 300 from you on Google Pay. On approving, your a/c will be debited",
    "Your HDFC Bank Credit Card XX9012 bill of Rs 4,500 is due on 05-Nov-26",
    "Congratulations! You are pre-approved for a Personal Loan of Rs 5,00,000. Apply now",
    "ICICI BANK NEFT Transaction with reference number IN0000123 for Rs. 2,500.00 has been credited to the beneficiary account on 21-10-2026",
    "RTGS Money Deposited~INR 2,00,000.00~To SAMPLE NAME~Txn No: HDFCR000111~On 21-10-2026~-HDFC Bank",
]


class BankFormatTests(unittest.TestCase):
    def test_hdfc_and_icici_formats(self):
        for sms, kind, amount, balance, account, ref, merchant, category in BANK_SMS:
            with self.subTest(sms=sms[:60]):
                p = parse_sms_with_regex(sms)
                self.assertIsNotNone(p)
                self.assertEqual(p.type, kind)
                self.assertEqual(p.amount, Decimal(amount))
                self.assertEqual(p.balance, Decimal(balance) if balance else None)
                self.assertEqual(p.account_ref, account)
                self.assertEqual(p.reference_id, ref)
                self.assertEqual(p.merchant, merchant)
                self.assertEqual(p.category, category)

    def test_sender_identifies_bank(self):
        p = parse_sms_with_regex("Rs.500 debited from a/c **1234 on 21-10-26 to VPA zepto@ybl", sender="JD-HDFCBK-S")
        self.assertEqual(p.account_ref, "HDFC XX1234")
        self.assertEqual(p.category, "Groceries")

    def test_amount_at_sentence_end_is_not_truncated(self):
        p = parse_sms_with_regex("Your a/c XX1234 is debited with Rs.1,234.56.")
        self.assertEqual(p.amount, Decimal("1234.56"))

    def test_helpline_numbers_are_not_merchants(self):
        p = parse_sms_with_regex("HDFC Bank:Rs. 1.00 debited from a/c *1234 on 07/09/26 to a/c **5678 (UPI Ref No. 629400009999). Not you? Call on 18002586161 to report")
        self.assertIsNone(p.merchant)
        self.assertEqual(p.reference_id, "629400009999")


class IgnoreTests(unittest.IsolatedAsyncioTestCase):
    async def test_non_transactions_are_ignored(self):
        for sms in NOT_TRANSACTIONS:
            with self.subTest(sms=sms[:60]):
                self.assertEqual((await parse_sms(sms)).parsed_by, "ignored")

    async def test_offers_are_ignored_without_asking_ai(self):
        offers = [
            # The two HDFC texts that were recorded as +Rs.50 refunds
            "Welcome Bonus\nGet 10% cashback up to Rs.50 on your first load of min Rs.100 on HDFC Bank Digital Rupee app. Load: https://1.hdfc.bank.in/HDFCBK/s/AAy9mNlw T&C",
            "Welcome Bonus\nGet 10% cashback up to Rs.50 on your first load of min Rs.100 on HDFC Bank Digital Rupee app. Load: https://1.hdfc.bank.in/HDFCBK/s/Ov15aw2x T&C",
            "Get flat Rs.150 cashback on your first bill payment via HDFC Bank PayZapp. Use code PAY150. T&C apply",
            "Shop on Amazon with your ICICI Bank Credit Card & earn up to 10% cashback. Offer valid till 31-Oct. T&C",
            "Congratulations! You have won a cashback voucher worth Rs.100. Claim now: http://icici.co/x T&Cs apply",
            "Enjoy 5% cashback on groceries this weekend with HDFC Bank Debit Card. Min txn Rs.500. Valid till Sunday",
        ]
        with patch.dict(os.environ, {"GEMINI_API_KEY": "test"}), \
                patch("parser.categorize_with_gemini", new_callable=AsyncMock) as ai, \
                patch("parser.parse_sms_with_gemini", new_callable=AsyncMock) as full_ai:
            for sms in offers:
                with self.subTest(sms=sms[:60]):
                    self.assertEqual((await parse_sms(sms)).parsed_by, "ignored")
        ai.assert_not_called()
        full_ai.assert_not_called()

    async def test_real_cashback_still_counts(self):
        for sms, amount in [
            ("Cashback of Rs.50.00 has been credited to your HDFC Bank A/c XX4821 for your Digital Rupee load. Ref 629400001111", 50),
            ("Rs 25.00 cashback received in your ICICI Bank Acct XX310 on 28-Oct-26. UPI Ref 629400002222", 25),
            ("Congratulations! Rs.100 cashback credited to your HDFC Bank A/c XX4821 on 28-10-26 for the Welcome Bonus offer", 100),
        ]:
            with self.subTest(sms=sms[:60]):
                p = await parse_sms(sms)
                self.assertEqual((p.type, p.amount, p.category, p.is_parsed), ("CREDIT", Decimal(amount), "Refunds & Cashback", True))

    async def test_refund_of_failed_transaction_is_recorded(self):
        p = await parse_sms("Rs.500 credited to HDFC Bank A/c XX1234 as refund for failed UPI txn 629400001234")
        self.assertEqual((p.type, p.amount, p.category), ("CREDIT", Decimal(500), "Refunds & Cashback"))


class SalaryAndIncomeTests(unittest.TestCase):
    def test_salary_and_other_income_are_separate(self):
        cases = [
            ("Rs.92,500.00 credited to HDFC Bank A/c XX4821 on 31-10-26 towards SALARY for OCT-26 from ACME TECH PVT LTD", "Salary"),
            ("ICICI Bank Acct XX310 credited with Rs 88,000.00 on 30-Oct-26; NEFT-SAL CR-ACME TECHNOLOGIES. Avl Bal Rs 1,02,000.00", "Salary"),
            ("Rs.412.00 credited to HDFC Bank A/c XX4821 on 30-09-26 as Savings Interest. Avl bal Rs 50,412.00", "Income"),
            ("Dividend of Rs 250.00 credited to ICICI Bank Acct XX310 on 12-Oct-26 from INFOSYS LTD", "Income"),
        ]
        for sms, category in cases:
            with self.subTest(sms=sms[:50]):
                p = parse_sms_with_regex(sms)
                self.assertEqual((p.type, p.category), ("CREDIT", category))

    def test_spending_that_mentions_interest_is_not_income(self):
        p = parse_sms_with_regex("Rs.1,250.00 debited from HDFC Bank A/c XX4821 on 05-10-26 towards loan interest. Ref 629400003333")
        self.assertEqual(p.type, "DEBIT")
        self.assertNotIn(p.category, ("Income", "Salary"))

    def test_old_combined_category_maps_to_salary(self):
        self.assertEqual(normalize_category("Salary & Income"), "Salary")
        self.assertIn("Income", CATEGORIES)
        self.assertNotIn("Salary & Income", CATEGORIES)


class CategoryFallbackTests(unittest.IsolatedAsyncioTestCase):
    SMS = "ICICI Bank Acct XX310 debited for Rs 250.00 on 09-Oct-26; A.B. Traders credited. UPI:629400000001."

    async def test_uncategorized_regex_result_asks_ai_for_category(self):
        with patch.dict(os.environ, {"GEMINI_API_KEY": "test"}), \
                patch("parser.categorize_with_gemini", new_callable=AsyncMock, return_value="Shopping") as ai, \
                patch("parser.parse_sms_with_gemini", new_callable=AsyncMock) as full_ai:
            p = await parse_sms(self.SMS)
        ai.assert_awaited_once_with(self.SMS, "A.B. Traders", "DEBIT")
        full_ai.assert_not_called()
        self.assertEqual((p.category, p.parsed_by, p.amount), ("Shopping", "regex+ai", Decimal(250)))

    async def test_ai_unknown_or_error_falls_back_to_uncategorized(self):
        for outcome in [{"return_value": "Uncategorized"}, {"side_effect": TimeoutError}]:
            with patch.dict(os.environ, {"GEMINI_API_KEY": "test"}), \
                    patch("parser.categorize_with_gemini", new_callable=AsyncMock, **outcome):
                p = await parse_sms(self.SMS)
            self.assertEqual((p.category, p.parsed_by), ("Uncategorized", "regex"))

    async def test_known_merchant_does_not_call_ai(self):
        with patch.dict(os.environ, {"GEMINI_API_KEY": "test"}), \
                patch("parser.categorize_with_gemini", new_callable=AsyncMock) as ai:
            p = await parse_sms(BANK_SMS[0][0])
        ai.assert_not_called()
        self.assertEqual(p.category, "Food & Dining")

    async def test_no_api_key_skips_ai(self):
        with patch("parser.categorize_with_gemini", new_callable=AsyncMock) as ai:
            p = await parse_sms(self.SMS)
        ai.assert_not_called()
        self.assertEqual(p.category, "Uncategorized")

    async def test_ai_full_parse_rejects_non_transactions_and_normalizes(self):
        from parser import _AIParse
        responses = [
            _AIParse(is_transaction=False),
            _AIParse(is_transaction=True, type="debit", amount=99.5, currency="Rs", merchant="Chai Point",
                     category="food & dining", account_ref="XXXX4321"),
            _AIParse(is_transaction=True, type="DEBIT", amount=10, category="Groceries & Stuff"),
        ]
        with patch("parser._ask_gemini", new_callable=AsyncMock, side_effect=responses):
            ignored = await parse_sms("Your balance enquiry: available balance Rs 100", "AD-HDFCBK")
            parsed = await parse_sms("weird bank format 99.5", "AD-HDFCBK")
            unknown = await parse_sms("another weird format 10")
        self.assertEqual(ignored.parsed_by, "ignored")
        self.assertEqual((parsed.type, parsed.amount, parsed.currency, parsed.category, parsed.account_ref),
                         ("DEBIT", Decimal("99.5"), "INR", "Food & Dining", "HDFC XX4321"))
        self.assertEqual(unknown.category, "Uncategorized")

    def test_normalize_category(self):
        self.assertEqual(normalize_category(" transfer "), "Transfer")
        self.assertEqual(normalize_category("General"), "Uncategorized")
        self.assertEqual(normalize_category(None), "Uncategorized")
        self.assertIn("Uncategorized", CATEGORIES)


if __name__ == "__main__":
    unittest.main()
