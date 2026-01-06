"""
Beancount Export - Generate beancount-compatible ledger entries.

Exports categorized transactions to beancount plain-text accounting format.
"""

import re
import subprocess
import tempfile
from typing import Dict, List, Optional

# Common currency symbols to ISO 4217 codes
SYMBOL_TO_CODE = {
    '$': 'USD',
    '€': 'EUR',
    '£': 'GBP',
    '¥': 'JPY',
    'zł': 'PLN',
    'zl': 'PLN',
    '₹': 'INR',
    '₽': 'RUB',
    'kr': 'SEK',  # Could be SEK, NOK, DKK - default to SEK
    'CHF': 'CHF',
    'R$': 'BRL',
    '₩': 'KRW',
    'A$': 'AUD',
    'C$': 'CAD',
}


def extract_currency_code(currency_format: str) -> str:
    """Extract ISO currency code from a currency_format string.

    Args:
        currency_format: Format string like "${amount}", "€{amount}", "{amount} zł"

    Returns:
        ISO 4217 currency code (e.g., "USD", "EUR", "PLN")

    Examples:
        >>> extract_currency_code("${amount}")
        'USD'
        >>> extract_currency_code("€{amount}")
        'EUR'
        >>> extract_currency_code("{amount} zł")
        'PLN'
    """
    # Remove the {amount} placeholder to get just the currency part
    currency_part = currency_format.replace('{amount}', '').strip()

    # Check for known symbols
    for symbol, code in SYMBOL_TO_CODE.items():
        if symbol in currency_part:
            return code

    # If no match, check if currency_part itself looks like an ISO code
    if len(currency_part) == 3 and currency_part.isalpha():
        return currency_part.upper()

    # Default fallback
    return 'USD'


def normalize_beancount_period(period: str) -> str:
    """Normalize a beancount period string.

    Args:
        period: Period string like "2025" or "2025-11"

    Returns:
        Normalized period string (YYYY or YYYY-MM)
    """
    if period is None:
        raise ValueError("Beancount period must be YYYY or YYYY-MM.")

    value = period.strip()
    if not value:
        raise ValueError("Beancount period must be YYYY or YYYY-MM.")

    match = re.match(r'^(?P<year>\d{4})(?:-(?P<month>\d{1,2}))?$', value)
    if not match:
        raise ValueError(f"Invalid beancount period '{period}'. Use YYYY or YYYY-MM.")

    year = int(match.group('year'))
    month = match.group('month')
    if month is None:
        return f"{year:04d}"

    month_num = int(month)
    if month_num < 1 or month_num > 12:
        raise ValueError(f"Invalid beancount period '{period}'. Month must be 01-12.")

    return f"{year:04d}-{month_num:02d}"


def _sanitize_account_name(name: str) -> str:
    """Sanitize a string to be a valid beancount account component.

    Beancount accounts must start with uppercase and contain only letters/numbers.

    Args:
        name: Raw name (e.g., "Dining Out", "401k")

    Returns:
        Valid account component (e.g., "DiningOut", "K401")
    """
    if not name:
        return 'Other'

    # Handle names starting with numbers (e.g., "401k" -> "K401")
    if name[0].isdigit():
        name = 'X' + name

    # Remove invalid characters, keep alphanumeric
    result = ''.join(c if c.isalnum() else '' for c in name.title().replace(' ', ''))

    # Ensure starts with uppercase
    if result and not result[0].isupper():
        result = result[0].upper() + result[1:]

    return result or 'Other'


def _get_expense_account(
    category: str,
    subcategory: str,
    tags: List[str],
    account_mappings: Dict,
    default_expense: str,
) -> str:
    """Map category/subcategory to beancount expense account.

    Args:
        category: Transaction category (e.g., "Subscriptions")
        subcategory: Transaction subcategory (e.g., "Streaming")
        tags: List of tags from merchant rules
        account_mappings: User-configured category -> account mappings
        default_expense: Default expense account if no mapping found

    Returns:
        Beancount account string (e.g., "Expenses:Subscriptions:Streaming")
    """
    tags_lower = {t.lower() for t in tags} if tags else set()

    # Check for special tags first
    if 'income' in tags_lower:
        # Check for explicit mapping first
        if '_income' in account_mappings:
            mapping = account_mappings['_income']
            if isinstance(mapping, dict):
                # Support subcategory-level income mappings
                if subcategory and subcategory in mapping:
                    return mapping[subcategory]
                if category and category in mapping:
                    return mapping[category]
                if '_default' in mapping:
                    return mapping['_default']
            elif isinstance(mapping, str):
                return mapping
        # Auto-generate from category/subcategory
        if category and subcategory:
            return f"Income:{_sanitize_account_name(category)}:{_sanitize_account_name(subcategory)}"
        if category:
            return f"Income:{_sanitize_account_name(category)}"
        return 'Income:Other'

    if 'transfer' in tags_lower:
        # Check for explicit mapping first
        if '_transfer' in account_mappings:
            mapping = account_mappings['_transfer']
            if isinstance(mapping, str):
                return mapping
            if isinstance(mapping, dict):
                if category in mapping:
                    if isinstance(mapping[category], str):
                        return mapping[category]
                    mapping_cat = mapping[category]
                    if subcategory and subcategory in mapping_cat:
                        return mapping_cat[subcategory]
                # Use category-level default if available
                if '_default' in mapping:
                    return mapping['_default']
        # Auto-generate from category/subcategory as bank account
        if category and subcategory:
            return f"Assets:Bank:{_sanitize_account_name(subcategory)}"
        if subcategory:
            return f"Assets:Bank:{_sanitize_account_name(subcategory)}"
        return 'Assets:Bank:FIXME'

    if 'investment' in tags_lower:
        # Use category/subcategory for investment accounts too
        if '_investment' in account_mappings:
            return account_mappings['_investment']
        if category and subcategory:
            return f"Assets:Investments:{_sanitize_account_name(subcategory)}"
        if subcategory:
            return f"Assets:Investments:{_sanitize_account_name(subcategory)}"
        return 'Assets:Investments'

    # Look up in mappings
    if category in account_mappings:
        mapping = account_mappings[category]
        if isinstance(mapping, str):
            return mapping
        if isinstance(mapping, dict):
            if subcategory and subcategory in mapping:
                return mapping[subcategory]
            # Use category-level default if available
            if '_default' in mapping:
                return mapping['_default']

    # Auto-generate from category/subcategory
    if category and subcategory:
        return f"Expenses:{_sanitize_account_name(category)}:{_sanitize_account_name(subcategory)}"
    if category:
        return f"Expenses:{_sanitize_account_name(category)}"

    return default_expense


def _get_asset_account(
    source: str,
    source_accounts: Dict[str, str],
    default_asset: str,
) -> str:
    """Get asset account for a transaction source.

    Args:
        source: Data source name (e.g., "AMEX", "Chase")
        source_accounts: Mapping of source names to asset accounts
        default_asset: Default asset account if no mapping found

    Returns:
        Beancount account string (e.g., "Liabilities:CreditCard:Amex")
    """
    if source and source in source_accounts:
        return source_accounts[source]
    return default_asset


def _escape_string(s: str) -> str:
    """Escape a string for beancount (backslashes and double quotes)."""
    if not s:
        return ''
    # Escape backslashes first, then quotes
    return s.replace('\\', '\\\\').replace('"', '\\"')


def export_beancount(
    stats: Dict,
    config: Dict,
    category_filter: Optional[str] = None,
    period_filter: Optional[str] = None,
) -> str:
    """Export transactions as beancount ledger entries.

    Args:
        stats: Analysis results from analyze_transactions()
        config: Loaded settings with account mappings
        category_filter: Optional category filter

    Returns:
        Beancount ledger file content as string
    """
    lines = []
    by_merchant = stats.get('by_merchant', {})

    # Get beancount config section
    bc_config = config.get('beancount', {})

    # Currency: explicit config > extracted from currency_format > USD
    currency = bc_config.get('currency')
    if not currency:
        currency_format = config.get('currency_format', '${amount}')
        currency = extract_currency_code(currency_format)

    # Account settings
    default_expense = bc_config.get('default_expense_account', 'Expenses:FIXME')
    default_asset = bc_config.get('default_asset_account', 'Assets:FIXME')
    account_mappings = bc_config.get('account_mappings', {})
    source_accounts = bc_config.get('source_accounts', {})

    # Build source -> currency mapping from data_sources config
    source_currencies = {}
    for source in config.get('data_sources', []):
        source_name = source.get('name', '')
        source_currency = source.get('currency')
        if source_name and source_currency:
            source_currencies[source_name] = source_currency

    # Normalize period filter (YYYY or YYYY-MM)
    normalized_period = (
        normalize_beancount_period(period_filter)
        if period_filter is not None
        else None
    )

    def _matches_period(txn_month: str) -> bool:
        if not normalized_period:
            return True
        if not txn_month:
            return False
        if len(normalized_period) == 4:
            return txn_month.startswith(f"{normalized_period}-")
        return txn_month == normalized_period

    # Collect all transactions across merchants
    all_txns = []
    for merchant_name, data in by_merchant.items():
        # Apply category filter
        if category_filter and data.get('category') != category_filter:
            continue

        for txn in data.get('transactions', []):
            if not _matches_period(txn.get('month', '')):
                continue
            all_txns.append({
                **txn,
                'merchant': merchant_name,
                'category': data.get('category', ''),
                'subcategory': data.get('subcategory', ''),
                'merchant_tags': list(data.get('tags', [])),
            })

    # Sort by date (month + day), then by merchant for consistent ordering
    # Incomplete transactions are sorted to the bottom
    def sort_key(t):
        # month is "YYYY-MM", date is "MM/DD"
        month = t.get('month', '2000-01')
        day = t.get('date', '01/01').split('/')[1] if '/' in t.get('date', '') else '01'
        # Check if transaction is incomplete (sort to bottom)
        all_tags = t.get('merchant_tags', []) + t.get('tags', [])
        is_incomplete = 1 if any(tag.lower() == 'incomplete' for tag in all_tags) else 0
        # Primary: incomplete status (0=normal, 1=incomplete), then date, then merchant
        return (is_incomplete, f"{month}-{day.zfill(2)}", t.get('merchant', ''))

    all_txns.sort(key=sort_key)

    # Detect and merge currency exchange pairs
    # These are pairs where: same date, same merchant, same accounts, different currencies,
    # one positive and one negative amount
    merged_exchanges = set()  # Track indices of transactions merged into exchanges

    def _get_full_date(txn):
        """Extract full date string from transaction."""
        month_part = txn.get('month', '2000-01')
        date_part = txn.get('date', '01/01')
        try:
            day = date_part.split('/')[1]
        except (IndexError, AttributeError):
            day = '01'
        return f"{month_part}-{day.zfill(2)}"

    def _get_txn_currency(txn):
        """Get currency for a transaction."""
        src = txn.get('source', '')
        return source_currencies.get(src, currency)

    def _is_exchange_pair(txn1, txn2):
        """Check if two transactions form a currency exchange pair."""
        # Must have same merchant and date
        if txn1.get('merchant') != txn2.get('merchant'):
            return False
        if _get_full_date(txn1) != _get_full_date(txn2):
            return False

        # Must have different currencies
        curr1 = _get_txn_currency(txn1)
        curr2 = _get_txn_currency(txn2)
        if curr1 == curr2:
            return False

        # Must have opposite signs (one debit, one credit)
        amt1 = txn1.get('amount', 0)
        amt2 = txn2.get('amount', 0)
        if not ((amt1 > 0 and amt2 < 0) or (amt1 < 0 and amt2 > 0)):
            return False

        # Must resolve to same accounts (both transfers to same account)
        all_tags1 = txn1.get('merchant_tags', []) + txn1.get('tags', [])
        all_tags2 = txn2.get('merchant_tags', []) + txn2.get('tags', [])
        tags_lower1 = {t.lower() for t in all_tags1} if all_tags1 else set()
        tags_lower2 = {t.lower() for t in all_tags2} if all_tags2 else set()

        # Both should be transfers for this to be a currency exchange
        if 'transfer' not in tags_lower1 or 'transfer' not in tags_lower2:
            return False

        # Check that expense (target) accounts match
        exp1 = _get_expense_account(
            txn1['category'], txn1['subcategory'], all_tags1,
            account_mappings, default_expense)
        exp2 = _get_expense_account(
            txn2['category'], txn2['subcategory'], all_tags2,
            account_mappings, default_expense)
        if exp1 != exp2:
            return False

        # Check that asset (source) accounts match
        asset1 = _get_asset_account(txn1.get('source', ''), source_accounts, default_asset)
        asset2 = _get_asset_account(txn2.get('source', ''), source_accounts, default_asset)
        if asset1 != asset2:
            return False

        return True

    # Find exchange pairs
    exchange_pairs = []  # List of (idx1, idx2) tuples
    for i, txn1 in enumerate(all_txns):
        if i in merged_exchanges:
            continue
        for j, txn2 in enumerate(all_txns):
            if j <= i or j in merged_exchanges:
                continue
            if _is_exchange_pair(txn1, txn2):
                exchange_pairs.append((i, j))
                merged_exchanges.add(i)
                merged_exchanges.add(j)
                break

    # Output exchange transactions first (they'll be re-sorted by date with regular txns)
    exchange_lines = []
    for idx1, idx2 in exchange_pairs:
        txn1, txn2 = all_txns[idx1], all_txns[idx2]
        full_date = _get_full_date(txn1)

        # Prepare payee and narration from first transaction
        payee = _escape_string(txn1['merchant'])
        raw_narration = txn1.get('description', '')
        cat = txn1.get('category', '')
        subcat = txn1.get('subcategory', '')
        if cat and subcat:
            narration = f"{_sanitize_account_name(cat)}:{_sanitize_account_name(subcat)} {raw_narration}"
        elif cat:
            narration = f"{_sanitize_account_name(cat)} {raw_narration}"
        else:
            narration = raw_narration
        narration = _escape_string(narration)

        # Get the common account
        all_tags = txn1.get('merchant_tags', []) + txn1.get('tags', [])
        target_account = _get_expense_account(
            txn1['category'], txn1['subcategory'], all_tags,
            account_mappings, default_expense)

        # Get amounts and currencies
        amt1 = txn1.get('amount', 0)
        amt2 = txn2.get('amount', 0)
        curr1 = _get_txn_currency(txn1)
        curr2 = _get_txn_currency(txn2)

        # Determine which is the "from" (what we're selling) and "to" (what we're buying) leg
        # In tally, positive = outflow (money leaving), negative = inflow (money received)
        # So positive amount is what we're selling, negative is what we're buying
        if amt1 > 0:
            from_amt, from_curr = amt1, curr1  # Selling this
            to_amt, to_curr = amt2, curr2       # Buying this
        else:
            from_amt, from_curr = amt2, curr2  # Selling this
            to_amt, to_curr = amt1, curr1       # Buying this

        # Build the exchange transaction with @@ price annotation for beancount
        # Format: Assets:Bank  -3300.00 DKK @@ 440.53 EUR (selling DKK for EUR)
        exchange_lines.append((full_date, f'{full_date} * "{payee}" "{narration}"'))
        exchange_lines.append((full_date, f'  {target_account}  {-abs(from_amt):.2f} {from_curr} @@ {abs(to_amt):.2f} {to_curr}'))
        exchange_lines.append((full_date, f'  {target_account}  {abs(to_amt):.2f} {to_curr}'))
        exchange_lines.append((full_date, ''))

    # Separate lists for normal and incomplete transactions
    incomplete_lines = []

    for txn_idx, txn in enumerate(all_txns):
        # Skip transactions that were merged into exchanges
        if txn_idx in merged_exchanges:
            continue
        # Check for skip tag - skip transactions tagged with 'skip'
        all_tags = txn.get('merchant_tags', []) + txn.get('tags', [])
        tags_lower = {t.lower() for t in all_tags} if all_tags else set()
        if 'skip' in tags_lower:
            continue

        # Reconstruct full date from month (YYYY-MM) and date (MM/DD)
        month_part = txn.get('month', '2000-01')  # "2025-01"
        date_part = txn.get('date', '01/01')  # "01/15"

        # Parse day from MM/DD format
        try:
            day = date_part.split('/')[1]
        except (IndexError, AttributeError):
            day = '01'

        full_date = f"{month_part}-{day.zfill(2)}"

        # Get accounts
        expense_account = _get_expense_account(
            txn['category'],
            txn['subcategory'],
            txn.get('merchant_tags', []) + txn.get('tags', []),
            account_mappings,
            default_expense,
        )
        asset_account = _get_asset_account(
            txn.get('source', ''),
            source_accounts,
            default_asset,
        )

        # Prepare payee and narration
        payee = _escape_string(txn['merchant'])
        raw_narration = txn.get('description', '')

        # Prepend normalized category:subcategory to narration
        category = txn.get('category', '')
        subcategory = txn.get('subcategory', '')
        if category and subcategory:
            narration = f"{_sanitize_account_name(category)}:{_sanitize_account_name(subcategory)} {raw_narration}"
        elif category:
            narration = f"{_sanitize_account_name(category)} {raw_narration}"
        else:
            narration = raw_narration
        narration = _escape_string(narration)

        # Amount handling
        # In tally, amounts are normalized: income/investment are stored as positive
        # In beancount, income accounts need negative amounts (credits)
        amount = txn.get('amount', 0)
        # Note: all_tags and tags_lower already computed at start of loop

        if 'income' in tags_lower:
            # Income: flip sign (tally stores positive, beancount needs negative)
            amount = -abs(amount)

        # Determine transaction flag (* = cleared, ! = pending/incomplete)
        is_incomplete = 'incomplete' in tags_lower
        flag = '!' if is_incomplete else '*'

        # Get currency for this transaction (source-specific or default)
        source = txn.get('source', '')
        txn_currency = source_currencies.get(source, currency)

        # Build transaction lines
        txn_lines = []
        txn_lines.append(f'{full_date} {flag} "{payee}" "{narration}"')
        txn_lines.append(f'  {expense_account}  {amount:.2f} {txn_currency}')
        txn_lines.append(f'  {asset_account}')
        txn_lines.append('')

        # Add to appropriate list
        if is_incomplete:
            incomplete_lines.extend(txn_lines)
        else:
            lines.extend(txn_lines)

    # Add exchange transactions (after normal, before incomplete)
    for _, line in exchange_lines:
        lines.append(line)

    # Add incomplete transactions at the very end
    lines.extend(incomplete_lines)

    output = '\n'.join(lines)

    # Format with bean-format if available
    return format_beancount(output)


def format_beancount(content: str) -> str:
    """Format beancount content using bean-format if available.

    Args:
        content: Raw beancount ledger content

    Returns:
        Formatted content if bean-format is available, otherwise original content
    """
    if not content.strip():
        return content

    try:
        # Write to temp file, run bean-format --inplace, read back
        with tempfile.NamedTemporaryFile(mode='w', suffix='.beancount', delete=False) as f:
            f.write(content)
            temp_path = f.name

        result = subprocess.run(
            ['bean-format', '--in-place', temp_path],
            capture_output=True,
            text=True,
        )

        if result.returncode == 0:
            with open(temp_path, 'r') as f:
                return f.read()
        else:
            # bean-format failed, return original
            return content
    except FileNotFoundError:
        # uv or bean-format not installed, return original
        return content
    except Exception:
        # Any other error, return original
        return content
    finally:
        # Clean up temp file
        import os
        try:
            os.unlink(temp_path)
        except Exception:
            pass
