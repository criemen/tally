"""
Beancount Export - Generate beancount-compatible ledger entries.

Exports categorized transactions to beancount plain-text accounting format.
"""

import os
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


def _get_beancount_context(config: Dict) -> Dict:
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

    return {
        'currency': currency,
        'default_expense': default_expense,
        'default_asset': default_asset,
        'account_mappings': account_mappings,
        'source_accounts': source_accounts,
        'source_currencies': source_currencies,
    }


def _normalize_period_filter(period_filter: Optional[str]) -> Optional[str]:
    if period_filter is None:
        return None
    return normalize_beancount_period(period_filter)


def _matches_period(txn_month: str, normalized_period: Optional[str]) -> bool:
    if not normalized_period:
        return True
    if not txn_month:
        return False
    if len(normalized_period) == 4:
        return txn_month.startswith(f"{normalized_period}-")
    return txn_month == normalized_period


def _collect_beancount_transactions(
    stats: Dict,
    category_filter: Optional[str],
    normalized_period: Optional[str],
) -> List[Dict]:
    by_merchant = stats.get('by_merchant', {})
    all_txns = []
    for merchant_name, data in by_merchant.items():
        # Apply category filter
        if category_filter and data.get('category') != category_filter:
            continue

        for txn in data.get('transactions', []):
            if not _matches_period(txn.get('month', ''), normalized_period):
                continue
            all_txns.append({
                **txn,
                'merchant': merchant_name,
                'category': data.get('category', ''),
                'subcategory': data.get('subcategory', ''),
                'merchant_tags': list(data.get('tags', [])),
            })
    return all_txns


def _txn_tags(txn: Dict) -> tuple[list, set]:
    all_tags = txn.get('merchant_tags', []) + txn.get('tags', [])
    tags_lower = {t.lower() for t in all_tags} if all_tags else set()
    return all_tags, tags_lower


def _sort_beancount_transactions(all_txns: List[Dict]) -> None:
    # Sort by date (month + day), then by merchant for consistent ordering
    # Incomplete transactions are sorted to the bottom
    def sort_key(t):
        # month is "YYYY-MM", date is "MM/DD"
        month = t.get('month', '2000-01')
        day = t.get('date', '01/01').split('/')[1] if '/' in t.get('date', '') else '01'
        # Check if transaction is incomplete (sort to bottom)
        _, tags_lower = _txn_tags(t)
        is_incomplete = 1 if 'incomplete' in tags_lower else 0
        # Primary: incomplete status (0=normal, 1=incomplete), then date, then merchant
        return (is_incomplete, f"{month}-{day.zfill(2)}", t.get('merchant', ''))

    all_txns.sort(key=sort_key)


def _get_full_date(txn: Dict) -> str:
    """Extract full date string from transaction."""
    month_part = txn.get('month', '2000-01')
    date_part = txn.get('date', '01/01')
    try:
        day = date_part.split('/')[1]
    except (IndexError, AttributeError):
        day = '01'
    return f"{month_part}-{day.zfill(2)}"


def _get_txn_currency(txn: Dict, context: Dict) -> str:
    """Get currency for a transaction."""
    src = txn.get('source', '')
    return context['source_currencies'].get(src, context['currency'])


def _is_exchange_pair(txn1: Dict, txn2: Dict, context: Dict) -> bool:
    """Check if two transactions form a currency exchange pair."""
    # Must have same merchant and date
    if txn1.get('merchant') != txn2.get('merchant'):
        return False
    if _get_full_date(txn1) != _get_full_date(txn2):
        return False

    # Must have different currencies
    curr1 = _get_txn_currency(txn1, context)
    curr2 = _get_txn_currency(txn2, context)
    if curr1 == curr2:
        return False

    # Must have opposite signs (one debit, one credit)
    amt1 = txn1.get('amount', 0)
    amt2 = txn2.get('amount', 0)
    if not ((amt1 > 0 and amt2 < 0) or (amt1 < 0 and amt2 > 0)):
        return False

    # Must resolve to same accounts (both transfers to same account)
    all_tags1, tags_lower1 = _txn_tags(txn1)
    all_tags2, tags_lower2 = _txn_tags(txn2)

    # Both should be transfers for this to be a currency exchange
    if 'transfer' not in tags_lower1 or 'transfer' not in tags_lower2:
        return False

    # Check that expense (target) accounts match
    exp1 = _get_expense_account(
        txn1['category'], txn1['subcategory'], all_tags1,
        context['account_mappings'], context['default_expense'])
    exp2 = _get_expense_account(
        txn2['category'], txn2['subcategory'], all_tags2,
        context['account_mappings'], context['default_expense'])
    if exp1 != exp2:
        return False

    # Check that asset (source) accounts match
    asset1 = _get_asset_account(
        txn1.get('source', ''), context['source_accounts'], context['default_asset'])
    asset2 = _get_asset_account(
        txn2.get('source', ''), context['source_accounts'], context['default_asset'])
    if asset1 != asset2:
        return False

    return True


def _find_exchange_pairs(all_txns: List[Dict], context: Dict) -> tuple[list, set]:
    merged_exchanges = set()  # Track indices of transactions merged into exchanges
    exchange_pairs = []  # List of (idx1, idx2) tuples
    for i, txn1 in enumerate(all_txns):
        if i in merged_exchanges:
            continue
        for j, txn2 in enumerate(all_txns):
            if j <= i or j in merged_exchanges:
                continue
            if _is_exchange_pair(txn1, txn2, context):
                exchange_pairs.append((i, j))
                merged_exchanges.add(i)
                merged_exchanges.add(j)
                break
    return exchange_pairs, merged_exchanges


def _format_exchange_entry(txn1: Dict, txn2: Dict, context: Dict) -> tuple[str, List[str], bool]:
    full_date = _get_full_date(txn1)
    month_key = txn1.get('month', '2000-01')

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
    all_tags1, tags_lower1 = _txn_tags(txn1)
    all_tags2, tags_lower2 = _txn_tags(txn2)
    target_account = _get_expense_account(
        txn1['category'], txn1['subcategory'], all_tags1,
        context['account_mappings'], context['default_expense'])

    # Get amounts and currencies
    amt1 = txn1.get('amount', 0)
    amt2 = txn2.get('amount', 0)
    curr1 = _get_txn_currency(txn1, context)
    curr2 = _get_txn_currency(txn2, context)

    # Determine which is the "from" (what we're selling) and "to" (what we're buying) leg
    # In tally, positive = outflow (money leaving), negative = inflow (money received)
    # So positive amount is what we're selling, negative is what we're buying
    if amt1 > 0:
        from_amt, from_curr = amt1, curr1  # Selling this
        to_amt, to_curr = amt2, curr2       # Buying this
    else:
        from_amt, from_curr = amt2, curr2  # Selling this
        to_amt, to_curr = amt1, curr1       # Buying this

    entry_lines = [
        f'{full_date} * "{payee}" "{narration}"',
        f'  {target_account}  {-abs(from_amt):.2f} {from_curr} @@ {abs(to_amt):.2f} {to_curr}',
        f'  {target_account}  {abs(to_amt):.2f} {to_curr}',
        '',
    ]

    tags_lower = tags_lower1 | tags_lower2
    is_manual = 'incomplete' in tags_lower
    return month_key, entry_lines, is_manual


def _format_transaction_entry(txn: Dict, context: Dict) -> Optional[tuple[str, List[str], bool]]:
    all_tags, tags_lower = _txn_tags(txn)
    if 'skip' in tags_lower:
        return None

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
        all_tags,
        context['account_mappings'],
        context['default_expense'],
    )
    asset_account = _get_asset_account(
        txn.get('source', ''),
        context['source_accounts'],
        context['default_asset'],
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
    if 'income' in tags_lower:
        # Income: flip sign (tally stores positive, beancount needs negative)
        amount = -abs(amount)

    # Determine transaction flag (* = cleared, ! = pending/incomplete)
    is_incomplete = 'incomplete' in tags_lower
    flag = '!' if is_incomplete else '*'

    # Get currency for this transaction (source-specific or default)
    txn_currency = _get_txn_currency(txn, context)

    # Build transaction lines
    txn_lines = [
        f'{full_date} {flag} "{payee}" "{narration}"',
        f'  {expense_account}  {amount:.2f} {txn_currency}',
        f'  {asset_account}',
        '',
    ]

    return month_part, txn_lines, is_incomplete


def _ensure_trailing_newline(content: str) -> str:
    if content and not content.endswith('\n'):
        return content + '\n'
    return content


def _append_content(path: str, content: str) -> None:
    if not content:
        return
    os.makedirs(os.path.dirname(path), exist_ok=True)
    needs_newline = False
    if os.path.exists(path):
        try:
            with open(path, 'rb') as existing:
                existing.seek(-1, os.SEEK_END)
                last = existing.read(1)
                needs_newline = last not in (b'\n', b'\r')
        except OSError:
            needs_newline = False
    with open(path, 'a', encoding='utf-8') as handle:
        if needs_newline:
            handle.write('\n')
        handle.write(content)


def export_beancount_files(
    stats: Dict,
    config: Dict,
    output_dir: str,
    category_filter: Optional[str] = None,
    period_filter: Optional[str] = None,
) -> Dict[str, List[str]]:
    """Export transactions into per-month beancount files.

    Finalized transactions are written to YYYY/MM.bean (overwritten).
    Non-finalized transactions are appended to YYYY/MM-manual.bean.

    Returns:
        Dict with "final_files" and "manual_files" lists of written paths.
    """
    if output_dir is None or not str(output_dir).strip():
        raise ValueError("Beancount output directory is required.")

    context = _get_beancount_context(config)
    normalized_period = _normalize_period_filter(period_filter)
    all_txns = _collect_beancount_transactions(stats, category_filter, normalized_period)
    _sort_beancount_transactions(all_txns)
    exchange_pairs, merged_exchanges = _find_exchange_pairs(all_txns, context)

    lines_by_month: Dict[str, List[str]] = {}
    incomplete_lines_by_month: Dict[str, List[str]] = {}
    exchange_lines_by_month: Dict[str, List[str]] = {}
    exchange_manual_by_month: Dict[str, List[str]] = {}

    def _add_lines(target: Dict[str, List[str]], month_key: str, new_lines: List[str]) -> None:
        if not month_key:
            return
        target.setdefault(month_key, []).extend(new_lines)

    for txn_idx, txn in enumerate(all_txns):
        # Skip transactions that were merged into exchanges
        if txn_idx in merged_exchanges:
            continue
        entry = _format_transaction_entry(txn, context)
        if not entry:
            continue
        month_key, txn_lines, is_manual = entry
        if is_manual:
            _add_lines(incomplete_lines_by_month, month_key, txn_lines)
        else:
            _add_lines(lines_by_month, month_key, txn_lines)

    for idx1, idx2 in exchange_pairs:
        month_key, entry_lines, is_manual = _format_exchange_entry(
            all_txns[idx1], all_txns[idx2], context)
        if is_manual:
            _add_lines(exchange_manual_by_month, month_key, entry_lines)
        else:
            _add_lines(exchange_lines_by_month, month_key, entry_lines)

    final_files = []
    manual_files = []
    base_dir = os.path.abspath(output_dir)

    final_months = set(lines_by_month.keys()) | set(exchange_lines_by_month.keys())
    for month_key in final_months:
        month_lines = lines_by_month.get(month_key, [])
        exchange_lines = exchange_lines_by_month.get(month_key, [])
        if not month_lines and not exchange_lines:
            continue
        try:
            year, month = month_key.split('-', 1)
        except ValueError:
            continue
        output_path = os.path.join(base_dir, year, f"{month}.bean")
        formatted = format_beancount('\n'.join(month_lines + exchange_lines))
        formatted = _ensure_trailing_newline(formatted)
        os.makedirs(os.path.dirname(output_path), exist_ok=True)
        with open(output_path, 'w', encoding='utf-8') as handle:
            handle.write(formatted)
        final_files.append(output_path)

    manual_months = set(incomplete_lines_by_month.keys()) | set(exchange_manual_by_month.keys())
    for month_key in manual_months:
        month_lines = incomplete_lines_by_month.get(month_key, []) + exchange_manual_by_month.get(month_key, [])
        if not month_lines:
            continue
        try:
            year, month = month_key.split('-', 1)
        except ValueError:
            continue
        output_path = os.path.join(base_dir, year, f"{month}-manual.bean")
        content = _ensure_trailing_newline('\n'.join(month_lines))
        _append_content(output_path, content)
        manual_files.append(output_path)

    return {'final_files': final_files, 'manual_files': manual_files}


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
