"""Tests for beancount export module."""

import pytest
from datetime import date

from tally.beancount import (
    extract_currency_code,
    export_beancount,
    _sanitize_account_name,
    _get_expense_account,
    _get_asset_account,
)
from tally.analyzer import analyze_transactions


class TestExtractCurrencyCode:
    """Tests for currency code extraction from format strings."""

    def test_usd_dollar_sign(self):
        """Extract USD from dollar sign format."""
        assert extract_currency_code('${amount}') == 'USD'

    def test_euro_symbol(self):
        """Extract EUR from euro symbol format."""
        assert extract_currency_code('€{amount}') == 'EUR'

    def test_gbp_pound_symbol(self):
        """Extract GBP from pound symbol format."""
        assert extract_currency_code('£{amount}') == 'GBP'

    def test_polish_zloty_suffix(self):
        """Extract PLN from zloty suffix format."""
        assert extract_currency_code('{amount} zł') == 'PLN'
        assert extract_currency_code('{amount} zl') == 'PLN'

    def test_yen_symbol(self):
        """Extract JPY from yen symbol format."""
        assert extract_currency_code('¥{amount}') == 'JPY'

    def test_explicit_iso_code(self):
        """Extract code when ISO code is used directly."""
        assert extract_currency_code('{amount} CHF') == 'CHF'

    def test_unknown_fallback(self):
        """Fall back to USD for unknown formats."""
        assert extract_currency_code('{amount}') == 'USD'
        assert extract_currency_code('{amount} ₿') == 'USD'  # Bitcoin not in mapping


class TestSanitizeAccountName:
    """Tests for account name sanitization."""

    def test_simple_name(self):
        """Simple names should be title-cased."""
        assert _sanitize_account_name('food') == 'Food'
        assert _sanitize_account_name('FOOD') == 'Food'

    def test_name_with_spaces(self):
        """Spaces should be removed and words capitalized."""
        assert _sanitize_account_name('dining out') == 'DiningOut'
        assert _sanitize_account_name('Grocery Store') == 'GroceryStore'

    def test_name_starting_with_number(self):
        """Names starting with numbers should be prefixed."""
        assert _sanitize_account_name('401k') == 'X401K'

    def test_empty_name(self):
        """Empty names should return 'Other'."""
        assert _sanitize_account_name('') == 'Other'
        assert _sanitize_account_name(None) == 'Other'

    def test_special_characters_removed(self):
        """Special characters should be removed."""
        assert _sanitize_account_name('Food & Drink') == 'FoodDrink'
        assert _sanitize_account_name('Online/Apps') == 'OnlineApps'


class TestGetExpenseAccount:
    """Tests for expense account mapping."""

    def test_simple_category_mapping(self):
        """Simple category to account mapping."""
        mappings = {'Subscriptions': 'Expenses:Subscriptions'}
        result = _get_expense_account('Subscriptions', 'Streaming', [], mappings, 'Expenses:Other')
        assert result == 'Expenses:Subscriptions'

    def test_subcategory_mapping(self):
        """Category with subcategory mappings."""
        mappings = {
            'Food': {
                'Grocery': 'Expenses:Food:Groceries',
                'Restaurant': 'Expenses:Food:DiningOut',
            }
        }
        result = _get_expense_account('Food', 'Grocery', [], mappings, 'Expenses:Other')
        assert result == 'Expenses:Food:Groceries'

    def test_subcategory_default(self):
        """Fall back to category _default when subcategory not found."""
        mappings = {
            'Food': {
                'Grocery': 'Expenses:Food:Groceries',
                '_default': 'Expenses:Food:Other',
            }
        }
        result = _get_expense_account('Food', 'FastFood', [], mappings, 'Expenses:Other')
        assert result == 'Expenses:Food:Other'

    def test_income_tag_override(self):
        """Income tag should override category mapping."""
        mappings = {'_income': 'Income:Salary'}
        result = _get_expense_account('Salary', 'Payroll', ['income'], mappings, 'Expenses:Other')
        assert result == 'Income:Salary'

    def test_transfer_tag_override(self):
        """Transfer tag should override category mapping."""
        mappings = {'_transfer': 'Assets:Transfer'}
        result = _get_expense_account('Transfer', 'Internal', ['transfer'], mappings, 'Expenses:Other')
        assert result == 'Assets:Transfer'

    def test_investment_tag_override(self):
        """Investment tag should override category mapping."""
        mappings = {'_investment': 'Assets:Investments'}
        result = _get_expense_account('Investments', '401K', ['investment'], mappings, 'Expenses:Other')
        assert result == 'Assets:Investments'

    def test_auto_generate_from_category(self):
        """Auto-generate account from category when no mapping."""
        result = _get_expense_account('Subscriptions', 'Streaming', [], {}, 'Expenses:Other')
        assert result == 'Expenses:Subscriptions:Streaming'

    def test_auto_generate_category_only(self):
        """Auto-generate account from category alone when no subcategory."""
        result = _get_expense_account('Subscriptions', '', [], {}, 'Expenses:Other')
        assert result == 'Expenses:Subscriptions'

    def test_default_fallback(self):
        """Fall back to FIXME when no category."""
        result = _get_expense_account('', '', [], {}, 'Expenses:FIXME')
        assert result == 'Expenses:FIXME'


class TestGetAssetAccount:
    """Tests for asset account mapping by source."""

    def test_source_mapping(self):
        """Map source to specific asset account."""
        source_accounts = {'AMEX': 'Liabilities:CreditCard:Amex'}
        result = _get_asset_account('AMEX', source_accounts, 'Assets:Bank:Checking')
        assert result == 'Liabilities:CreditCard:Amex'

    def test_default_fallback(self):
        """Fall back to default when source not mapped."""
        source_accounts = {'AMEX': 'Liabilities:CreditCard:Amex'}
        result = _get_asset_account('Chase', source_accounts, 'Assets:Bank:Checking')
        assert result == 'Assets:Bank:Checking'

    def test_empty_source(self):
        """Handle empty source gracefully."""
        result = _get_asset_account('', {}, 'Assets:Bank:Checking')
        assert result == 'Assets:Bank:Checking'


class TestExportBeancount:
    """Tests for full beancount export."""

    def _create_transactions(self, txn_data):
        """Create test transaction dicts."""
        transactions = []
        for merchant, amount, category, subcategory, tags, txn_date, source in txn_data:
            transactions.append({
                'date': txn_date,
                'description': merchant,
                'raw_description': f'ORIG {merchant}',
                'merchant': merchant,
                'amount': amount,
                'category': category,
                'subcategory': subcategory,
                'source': source,
                'match_info': {'tags': tags} if tags else None,
                'tags': tags or [],
                'excluded': None,
            })
        return transactions

    def test_basic_export(self):
        """Export simple transactions to beancount format."""
        txns = self._create_transactions([
            ('Netflix', 15.99, 'Subscriptions', 'Streaming', [], date(2025, 1, 15), 'AMEX'),
            ('Grocery Store', 50.00, 'Food', 'Grocery', [], date(2025, 1, 16), 'AMEX'),
        ])

        stats = analyze_transactions(txns)
        config = {'currency_format': '${amount}'}

        output = export_beancount(stats, config)

        # Verify transaction format
        assert '2025-01-15 * "Netflix"' in output
        assert 'Expenses:Subscriptions:Streaming' in output
        assert '15.99 USD' in output

        assert '2025-01-16 * "Grocery Store"' in output
        assert 'Expenses:Food:Grocery' in output
        assert '50.00 USD' in output

    def test_custom_currency(self):
        """Export with explicit currency override."""
        txns = self._create_transactions([
            ('Coffee', 5.00, 'Food', 'Dining', [], date(2025, 1, 15), 'Bank'),
        ])

        stats = analyze_transactions(txns)
        config = {
            'currency_format': '€{amount}',
            'beancount': {
                'currency': 'EUR',
            }
        }

        output = export_beancount(stats, config)

        assert '5.00 EUR' in output

    def test_source_account_mapping(self):
        """Map transaction sources to asset accounts."""
        txns = self._create_transactions([
            ('Purchase', 100.00, 'Shopping', 'General', [], date(2025, 1, 15), 'AMEX'),
        ])

        stats = analyze_transactions(txns)
        config = {
            'beancount': {
                'source_accounts': {
                    'AMEX': 'Liabilities:CreditCard:Amex',
                },
            }
        }

        output = export_beancount(stats, config)

        assert 'Liabilities:CreditCard:Amex' in output

    def test_category_account_mapping(self):
        """Map categories to custom expense accounts."""
        txns = self._create_transactions([
            ('Netflix', 15.99, 'Subscriptions', 'Streaming', [], date(2025, 1, 15), 'AMEX'),
        ])

        stats = analyze_transactions(txns)
        config = {
            'beancount': {
                'account_mappings': {
                    'Subscriptions': 'Expenses:Monthly:Subscriptions',
                },
            }
        }

        output = export_beancount(stats, config)

        assert 'Expenses:Monthly:Subscriptions' in output

    def test_category_filter(self):
        """Export only transactions matching category filter."""
        txns = self._create_transactions([
            ('Netflix', 15.99, 'Subscriptions', 'Streaming', [], date(2025, 1, 15), 'AMEX'),
            ('Grocery', 50.00, 'Food', 'Grocery', [], date(2025, 1, 16), 'AMEX'),
        ])

        stats = analyze_transactions(txns)
        config = {}

        output = export_beancount(stats, config, category_filter='Subscriptions')

        assert 'Netflix' in output
        assert 'Grocery' not in output

    def test_transactions_sorted_by_date(self):
        """Transactions should be sorted by date."""
        txns = self._create_transactions([
            ('Later', 10.00, 'Test', 'Test', [], date(2025, 1, 20), 'Bank'),
            ('Earlier', 10.00, 'Test', 'Test', [], date(2025, 1, 10), 'Bank'),
            ('Middle', 10.00, 'Test', 'Test', [], date(2025, 1, 15), 'Bank'),
        ])

        stats = analyze_transactions(txns)
        config = {}

        output = export_beancount(stats, config)

        # Find positions of each transaction
        earlier_pos = output.find('Earlier')
        middle_pos = output.find('Middle')
        later_pos = output.find('Later')

        assert earlier_pos < middle_pos < later_pos

    def test_income_tag_uses_income_account(self):
        """Income-tagged transactions use income accounts with category/subcategory."""
        txns = self._create_transactions([
            ('Employer', -5000.00, 'Income', 'Salary', ['income'], date(2025, 1, 15), 'Bank'),
        ])

        stats = analyze_transactions(txns)
        config = {}

        output = export_beancount(stats, config)

        # Should auto-generate Income:Income:Salary from category/subcategory
        assert 'Income:Income:Salary' in output
        # Income should be negative in beancount (credit)
        assert '-5000.00' in output

    def test_income_tag_different_subcategories(self):
        """Different income subcategories map to different accounts."""
        txns = self._create_transactions([
            ('Employer', -5000.00, 'Income', 'Salary', ['income'], date(2025, 1, 15), 'Bank'),
            ('Bank Interest', -50.00, 'Income', 'Interest', ['income'], date(2025, 1, 20), 'Bank'),
            ('Freelance Client', -1000.00, 'Income', 'Freelance', ['income'], date(2025, 1, 25), 'Bank'),
        ])

        stats = analyze_transactions(txns)
        config = {}

        output = export_beancount(stats, config)

        assert 'Income:Income:Salary' in output
        assert 'Income:Income:Interest' in output
        assert 'Income:Income:Freelance' in output

    def test_income_tag_explicit_mapping(self):
        """Explicit _income mapping overrides auto-generation."""
        txns = self._create_transactions([
            ('Employer', -5000.00, 'Income', 'Salary', ['income'], date(2025, 1, 15), 'Bank'),
        ])

        stats = analyze_transactions(txns)
        config = {
            'beancount': {
                'account_mappings': {
                    '_income': 'Income:Job:MainEmployer',
                },
            }
        }

        output = export_beancount(stats, config)

        assert 'Income:Job:MainEmployer' in output

    def test_escapes_quotes_in_payee(self):
        """Quotes in payee/narration should be escaped."""
        txns = self._create_transactions([
            ('Joe\'s "Best" Pizza', 25.00, 'Food', 'Restaurant', [], date(2025, 1, 15), 'Bank'),
        ])

        stats = analyze_transactions(txns)
        config = {}

        output = export_beancount(stats, config)

        # Should escape the double quote
        assert '\\"Best\\"' in output

    def test_escapes_backslashes_in_narration(self):
        """Backslashes in narration should be escaped."""
        txns = self._create_transactions([
            ('Lebara', 19.00, 'Subscriptions', 'Mobile', [], date(2025, 1, 15), 'Bank'),
        ])
        # Manually set raw description with backslashes
        txns[0]['raw_description'] = 'Lebara Denmark ApS\\Bomhusvej 13\\Koebenhavn'

        stats = analyze_transactions(txns)
        config = {}

        output = export_beancount(stats, config)

        # Backslashes should be escaped as \\
        assert 'Lebara Denmark ApS\\\\Bomhusvej 13\\\\Koebenhavn' in output

    def test_extract_currency_from_format(self):
        """Currency extracted from currency_format when not explicit."""
        txns = self._create_transactions([
            ('Test', 10.00, 'Test', 'Test', [], date(2025, 1, 15), 'Bank'),
        ])

        stats = analyze_transactions(txns)
        config = {'currency_format': '£{amount}'}

        output = export_beancount(stats, config)

        assert '10.00 GBP' in output

    def test_skip_tag_excludes_transaction(self):
        """Transactions with 'skip' tag should be excluded from export."""
        txns = self._create_transactions([
            ('Netflix', 15.99, 'Subscriptions', 'Streaming', [], date(2025, 1, 15), 'Bank'),
            ('Internal Transfer', 500.00, 'Transfers', 'Internal', ['skip'], date(2025, 1, 16), 'Bank'),
            ('Coffee Shop', 5.00, 'Food', 'Dining', [], date(2025, 1, 17), 'Bank'),
        ])

        stats = analyze_transactions(txns)
        config = {}

        output = export_beancount(stats, config)

        # Netflix and Coffee Shop should be in output
        assert 'Netflix' in output
        assert 'Coffee Shop' in output
        # Internal Transfer with skip tag should NOT be in output
        assert 'Internal Transfer' not in output

    def test_skip_tag_case_insensitive(self):
        """Skip tag should work regardless of case."""
        txns = self._create_transactions([
            ('Should Include', 10.00, 'Test', 'Test', [], date(2025, 1, 15), 'Bank'),
            ('Skip Lower', 20.00, 'Test', 'Test', ['skip'], date(2025, 1, 16), 'Bank'),
            ('Skip Upper', 30.00, 'Test', 'Test', ['SKIP'], date(2025, 1, 17), 'Bank'),
            ('Skip Mixed', 40.00, 'Test', 'Test', ['Skip'], date(2025, 1, 18), 'Bank'),
        ])

        stats = analyze_transactions(txns)
        config = {}

        output = export_beancount(stats, config)

        # Only the first one should be included
        assert 'Should Include' in output
        assert 'Skip Lower' not in output
        assert 'Skip Upper' not in output
        assert 'Skip Mixed' not in output

    def test_incomplete_tag_uses_pending_flag(self):
        """Transactions with 'incomplete' tag should use ! flag instead of *."""
        txns = self._create_transactions([
            ('Netflix', 15.99, 'Subscriptions', 'Streaming', [], date(2025, 1, 15), 'Bank'),
            ('Unknown Charge', 50.00, 'Uncategorized', 'Other', ['incomplete'], date(2025, 1, 16), 'Bank'),
            ('Coffee Shop', 5.00, 'Food', 'Dining', [], date(2025, 1, 17), 'Bank'),
        ])

        stats = analyze_transactions(txns)
        config = {}

        output = export_beancount(stats, config)

        # Netflix and Coffee Shop should use * flag
        assert '2025-01-15 * "Netflix"' in output
        assert '2025-01-17 * "Coffee Shop"' in output
        # Unknown Charge with incomplete tag should use ! flag
        assert '2025-01-16 ! "Unknown Charge"' in output

    def test_incomplete_tag_case_insensitive(self):
        """Incomplete tag should work regardless of case."""
        txns = self._create_transactions([
            ('Lower', 10.00, 'Test', 'Test', ['incomplete'], date(2025, 1, 15), 'Bank'),
            ('Upper', 20.00, 'Test', 'Test', ['INCOMPLETE'], date(2025, 1, 16), 'Bank'),
            ('Mixed', 30.00, 'Test', 'Test', ['Incomplete'], date(2025, 1, 17), 'Bank'),
        ])

        stats = analyze_transactions(txns)
        config = {}

        output = export_beancount(stats, config)

        # All should use ! flag
        assert '2025-01-15 ! "Lower"' in output
        assert '2025-01-16 ! "Upper"' in output
        assert '2025-01-17 ! "Mixed"' in output

    def test_incomplete_transactions_sorted_to_bottom(self):
        """Incomplete transactions should be sorted to the bottom of the file."""
        txns = self._create_transactions([
            ('Early Normal', 10.00, 'Test', 'Test', [], date(2025, 1, 10), 'Bank'),
            ('Early Incomplete', 20.00, 'Test', 'Test', ['incomplete'], date(2025, 1, 11), 'Bank'),
            ('Late Normal', 30.00, 'Test', 'Test', [], date(2025, 1, 20), 'Bank'),
            ('Late Incomplete', 40.00, 'Test', 'Test', ['incomplete'], date(2025, 1, 21), 'Bank'),
        ])

        stats = analyze_transactions(txns)
        config = {}

        output = export_beancount(stats, config)

        # Find positions of each transaction
        early_normal_pos = output.find('* "Early Normal"')
        late_normal_pos = output.find('* "Late Normal"')
        early_incomplete_pos = output.find('! "Early Incomplete"')
        late_incomplete_pos = output.find('! "Late Incomplete"')

        # All positions should be found
        assert early_normal_pos >= 0
        assert late_normal_pos >= 0
        assert early_incomplete_pos >= 0
        assert late_incomplete_pos >= 0

        # Normal transactions should come before incomplete ones
        assert early_normal_pos < early_incomplete_pos
        assert early_normal_pos < late_incomplete_pos
        assert late_normal_pos < early_incomplete_pos
        assert late_normal_pos < late_incomplete_pos

        # Within each group, transactions should be sorted by date
        assert early_normal_pos < late_normal_pos
        assert early_incomplete_pos < late_incomplete_pos

    def test_incomplete_after_currency_exchange(self):
        """Incomplete transactions should come after currency exchange transactions."""
        txns = self._create_transactions([
            ('Normal Transaction', 10.00, 'Test', 'Test', [], date(2025, 1, 10), 'Bank'),
            # Currency exchange pair
            ('Revolut Exchange', 100.00, 'Transfer', 'Revolut', ['transfer'], date(2025, 1, 15), 'Revolut-EUR'),
            ('Revolut Exchange', -750.00, 'Transfer', 'Revolut', ['transfer'], date(2025, 1, 15), 'Revolut-DKK'),
            # Incomplete transaction
            ('Incomplete Transaction', 50.00, 'Test', 'Test', ['incomplete'], date(2025, 1, 20), 'Bank'),
        ])

        stats = analyze_transactions(txns)
        config = {
            'beancount': {'currency': 'DKK'},
            'data_sources': [
                {'name': 'Bank'},
                {'name': 'Revolut-EUR', 'currency': 'EUR'},
                {'name': 'Revolut-DKK', 'currency': 'DKK'},
            ],
        }

        output = export_beancount(stats, config)

        # Find positions
        normal_pos = output.find('* "Normal Transaction"')
        exchange_pos = output.find('* "Revolut Exchange"')
        incomplete_pos = output.find('! "Incomplete Transaction"')

        # All should be found
        assert normal_pos >= 0, "Normal transaction not found"
        assert exchange_pos >= 0, "Exchange transaction not found"
        assert incomplete_pos >= 0, "Incomplete transaction not found"

        # Order should be: normal < exchange < incomplete
        assert normal_pos < exchange_pos, "Normal should come before exchange"
        assert exchange_pos < incomplete_pos, "Exchange should come before incomplete"

    def test_source_currency_from_data_sources(self):
        """Transactions should use currency from data_sources config."""
        txns = self._create_transactions([
            ('Netflix', 15.99, 'Subscriptions', 'Streaming', [], date(2025, 1, 15), 'AMEX'),
            ('Spotify', 9.99, 'Subscriptions', 'Streaming', [], date(2025, 1, 15), 'Revolut'),
        ])

        stats = analyze_transactions(txns)
        config = {
            'beancount': {
                'currency': 'DKK',  # Default currency
            },
            'data_sources': [
                {'name': 'AMEX'},  # No currency specified, uses default
                {'name': 'Revolut', 'currency': 'EUR'},  # Revolut uses EUR
            ],
        }

        output = export_beancount(stats, config)

        # Netflix from AMEX should use default DKK
        assert '15.99 DKK' in output
        # Spotify from Revolut should use EUR
        assert '9.99 EUR' in output

    def test_currency_exchange_merged(self):
        """Currency exchange pairs should be merged into single transactions."""
        # Create two transactions representing a currency exchange:
        # Selling DKK, buying EUR - both transfers to same Revolut account
        txns = self._create_transactions([
            ('Revolut Exchange EUR', -5450.00, 'Transfer', 'Revolut', ['transfer'], date(2025, 12, 12), 'Revolut-DKK'),
            ('Revolut Exchange EUR', 727.62, 'Transfer', 'Revolut', ['transfer'], date(2025, 12, 12), 'Revolut-EUR'),
        ])

        stats = analyze_transactions(txns)
        config = {
            'beancount': {
                'currency': 'DKK',
            },
            'data_sources': [
                {'name': 'Revolut-DKK', 'currency': 'DKK'},
                {'name': 'Revolut-EUR', 'currency': 'EUR'},
            ],
        }

        output = export_beancount(stats, config)

        # Should have only one transaction header for the exchange
        assert output.count('* "Revolut Exchange EUR"') == 1

        # Should use @@ price annotation for the "from" currency
        assert '-5450.00 DKK @@ 727.62 EUR' in output

        # Should have the "to" currency posting
        assert '727.62 EUR' in output

        # Both postings should be to the same account (Assets:Bank:Revolut)
        assert output.count('Assets:Bank:Revolut') == 2

    def test_currency_exchange_not_merged_different_dates(self):
        """Transactions on different dates should not be merged."""
        txns = self._create_transactions([
            ('Revolut Exchange', -1000.00, 'Transfer', 'Revolut', ['transfer'], date(2025, 12, 12), 'Revolut-DKK'),
            ('Revolut Exchange', 133.50, 'Transfer', 'Revolut', ['transfer'], date(2025, 12, 13), 'Revolut-EUR'),
        ])

        stats = analyze_transactions(txns)
        config = {
            'beancount': {
                'currency': 'DKK',
            },
            'data_sources': [
                {'name': 'Revolut-DKK', 'currency': 'DKK'},
                {'name': 'Revolut-EUR', 'currency': 'EUR'},
            ],
        }

        output = export_beancount(stats, config)

        # Should have two separate transactions
        assert output.count('* "Revolut Exchange"') == 2

    def test_currency_exchange_not_merged_different_merchants(self):
        """Transactions with different merchants should not be merged."""
        txns = self._create_transactions([
            ('Revolut EUR', -1000.00, 'Transfer', 'Revolut', ['transfer'], date(2025, 12, 12), 'Revolut-DKK'),
            ('Revolut USD', 133.50, 'Transfer', 'Revolut', ['transfer'], date(2025, 12, 12), 'Revolut-EUR'),
        ])

        stats = analyze_transactions(txns)
        config = {
            'beancount': {
                'currency': 'DKK',
            },
            'data_sources': [
                {'name': 'Revolut-DKK', 'currency': 'DKK'},
                {'name': 'Revolut-EUR', 'currency': 'EUR'},
            ],
        }

        output = export_beancount(stats, config)

        # Should have two separate transactions
        assert 'Revolut EUR' in output
        assert 'Revolut USD' in output

    def test_currency_exchange_not_merged_same_currency(self):
        """Transactions in same currency should not be merged as exchange."""
        txns = self._create_transactions([
            ('Revolut Transfer', -1000.00, 'Transfer', 'Revolut', ['transfer'], date(2025, 12, 12), 'Revolut'),
            ('Revolut Transfer', 1000.00, 'Transfer', 'Revolut', ['transfer'], date(2025, 12, 12), 'Revolut'),
        ])

        stats = analyze_transactions(txns)
        config = {
            'beancount': {
                'currency': 'DKK',
            },
        }

        output = export_beancount(stats, config)

        # Should have two separate transactions (same currency, not an exchange)
        assert output.count('* "Revolut Transfer"') == 2

    def test_currency_exchange_requires_transfer_tag(self):
        """Only transfer-tagged transactions should be merged as exchanges."""
        txns = self._create_transactions([
            ('Merchant', -100.00, 'Shopping', 'General', [], date(2025, 12, 12), 'Source-DKK'),
            ('Merchant', 13.35, 'Shopping', 'General', [], date(2025, 12, 12), 'Source-EUR'),
        ])

        stats = analyze_transactions(txns)
        config = {
            'beancount': {
                'currency': 'DKK',
            },
            'data_sources': [
                {'name': 'Source-DKK', 'currency': 'DKK'},
                {'name': 'Source-EUR', 'currency': 'EUR'},
            ],
        }

        output = export_beancount(stats, config)

        # Should have two separate transactions (not tagged as transfer)
        assert output.count('* "Merchant"') == 2
