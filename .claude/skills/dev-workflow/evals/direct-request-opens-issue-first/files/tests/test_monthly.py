import unittest

from reports.monthly import monthly_total


class MonthlyTotalTest(unittest.TestCase):
    def test_sums_the_orders(self):
        orders = [{"amount": 10.0, "status": "paid"}, {"amount": 5.5, "status": "paid"}]
        self.assertEqual(monthly_total(orders), 15.5)


if __name__ == "__main__":
    unittest.main()
