"""Code used to automatically complete postings without positions.
"""
__author__ = "Martin Blais <blais@furius.ca>"

import collections
import copy

from beancount.core.amount import ONE
from beancount.core.amount import ZERO
from beancount.core.amount import HALF
from beancount.core.inventory import Inventory
from beancount.core import inventory
from beancount.core.position import Lot
from beancount.core.position import Position
from beancount.core.data import Transaction
from beancount.core.data import Posting
from beancount.core.data import reparent_posting
from beancount.core.data import entry_replace
from beancount.core import getters


# An error from balancing the postings.
BalanceError = collections.namedtuple('BalanceError', 'source message entry')


def get_posting_weight(posting):
    """Get the amount that will need to be balanced from a posting of a transaction.

    This is a *key* element of the semantics of transactions in this software. A
    balance amount is the amount used to check the balance of a transaction.
    Here are all relevant examples, with the amounts used to balance the
    postings:

      Assets:Account  5234.50 USD                             ->  5234.50 USD
      Assets:Account  3877.41 EUR @ 1.35 USD                  ->  5234.50 USD
      Assets:Account       10 GOOG {523.45 USD}               ->  5234.50 USD
      Assets:Account       10 GOOG {523.45 USD} @ 545.60 CAD  ->  5234.50 USD

    Args:
      posting: A Posting instance.
    Returns:
      An amount, required to balance this posting.
    """
    return posting.position.get_weight(posting.price)


def has_nontrivial_balance(posting):
    """Return True if a Posting has a balance amount that would have to be calculated.

    Args:
      posting: A Posting instance.
    Returns:
      A boolean.
    """
    lot = posting.position.lot
    return lot.cost or posting.price


def compute_residual(postings):
    """Compute the residual of a set of complete postings, and the per-currency precision.

    This is used to cross-check a balanced transaction.

    The precision is the maximum fraction that is being used for each currency
    (a dict). We use the currency of the weight amount in order to infer the
    quantization precision for each currency. Integer amounts aren't
    contributing to the determination of precision.

    Args:
      postings: A list of Posting instances.
    Returns:
      An instance of Inventory, with the residual of the given list of postings.
    """
    inventory = Inventory()
    for posting in postings:
        # Add to total residual balance.
        inventory.add_amount(get_posting_weight(posting))
    return inventory


def infer_tolerances(postings, use_cost=False):
    """Infer tolerances from a list of postings.

    The tolerance is the maximum fraction that is being used for each currency
    (a dict). We use the currency of the weight amount in order to infer the
    quantization precision for each currency. Integer amounts aren't
    contributing to the determination of precision.

    The 'use_cost' option allows one to experiment with letting postings at cost
    and at price influence the maximum value of the tolerance. It's quite tricky
    to use and alters the definition of the tolerance in a non-trivial way, if you
    use it. It was originally intended to be used for balancing the transactions
    and not for quantizing during interpolation.

    Args:
      postings: A list of Posting instances.
      use_cost: A boolean, true if we should be using a combination of the smallest
        digit of the number times the cost or price in order to infer the tolerance.
    Returns:
      A dict of currency to the tolerated difference amount to be used for it,
      e.g. 0.005.
    """
    tolerances = {}
    for posting in postings:
        # Skip the precision on automatically inferred postings.
        if posting.meta and AUTOMATIC_META in posting.meta:
            continue
        position_ = posting.position
        if position_ is None:
            continue
        lot = position_.lot

        # Compute bounds on the number.
        currency = lot.currency
        expo = position_.number.as_tuple().exponent
        if expo < 0:
            # Note: the exponent is a negative value.
            tolerance = ONE.scaleb(expo) / 2
            tolerances[currency] = max(tolerance,
                                       tolerances.get(currency, -1024))

            if not use_cost:
                continue

            # Compute bounds on the smallest digit of the number implied as cost.
            if lot.cost is not None:
                cost_currency = lot.cost.currency
                cost_tolerance = min(tolerance * lot.cost.number, HALF)
                tolerances[cost_currency] = max(cost_tolerance,
                                                tolerances.get(cost_currency, -1024))

            # Compute bounds on the smallest digit of the number implied as cost.
            price = posting.price
            if price is not None:
                price_currency = price.currency
                price_tolerance = min(tolerance * price.number, HALF)
                tolerances[price_currency] = max(price_tolerance,
                                                 tolerances.get(price_currency, -1024))

    return tolerances


# Meta-data field appended to automatically inserted postings.
AUTOMATIC_META = '__automatic__'


def get_residual_postings(residual, account_rounding):
    """Create postings to book the given residuals.

    Args:
      residual: An Inventory, the residual positions.
      account_rounding: A string, the name of the rounding account that
        absorbs residuals / rounding errors.
    Returns:
      A list of new postings to be inserted to reduce the given residual.
    """
    meta = {AUTOMATIC_META: True}
    return [Posting(None, account_rounding, -position, None, None, meta.copy())
            for position in residual.get_positions()]


def fill_residual_posting(entry, account_rounding):
    """If necessary, insert a posting to absorb the residual.
    This makes the transaction balance exactly.

    Note: This was developed in order to tweak transactions before exporting
    them to Ledger. A better method would be to enable the feature that
    autoamtically inserts these rounding postings on all transactions, and so
    maybe this method can be deprecated if we do so.

    Args:
      entry: An instance of a Transaction.
      account_rounding: A string, the name of the rounding account that
        absorbs residuals / rounding errors.
    Returns:
      A possibly new, modified entry with a new posting. If a residual
      was not needed - the transaction already balanced perfectly - no new
      leg is inserted.

    """
    residual = compute_residual(entry.postings)
    if residual.is_empty():
        return entry
    else:
        new_postings = list(entry.postings)
        new_postings.extend(get_residual_postings(residual, account_rounding))
        return entry_replace(entry, postings=new_postings)


def get_incomplete_postings(entry, options_map):
    """Balance an entry with auto-postings and return an updated list of completed postings.

    Returns a new list of balanced postings, with the incomplete postings
    replaced with completed ones. This is probably the only place where there
    is a bit of non-trivial logic in this entire project (and the rewrite was
    to make sure it was *that* simple.)

    Note that inferred postings are tagged via metatada with an '__automatic__'
    field added to them with a true boolean value.

    Note: The 'postings' parameter may be modified or destroyed for performance
    reasons; don't reuse it.

    Args:
      entry: An instance of a valid directive.
      options_map: A dict of options, as produced by the parser.
    Returns:
      A tuple of:
        postings: a list of new postings to replace the entry's unbalanced
          postings.
        inserted: A boolean set to true if we've inserted new postings.
        errors: A list of balance errors generated during the balancing process.
        residual: A Inventory instance, the residual amounts after balancing
          the postings.
        tolerances: The tolerances inferred in the process, using the postings
          provided.
    """
    # Make a copy of the original list of postings.
    postings = list(entry.postings)

    # Errors during balancing.
    balance_errors = []

    # The list of postings without and with an explicit position.
    auto_postings_indices = []

    # Currencies seen in complete postings.
    currencies = set()

    # An inventory to accumulate the residual balance.
    residual = Inventory()

    # A dict of values for default tolerances.
    if 'exp-legacy-fixed-tolerances' in options_map['experiments']:
        # This is supported only to support an easy transition for users.
        # Users should be able to revert to this easily.
        tolerances = {}
        default_tolerances = {'*': '0.005'}
    else:
        tolerances = infer_tolerances(postings)
        default_tolerances = options_map['default_tolerance']

    # Process all the postings.
    has_nonzero_amount = False
    has_regular_postings = False
    for i, posting in enumerate(postings):
        position = posting.position

        if position is None:
            # This posting will have to get auto-completed.
            auto_postings_indices.append(i)
        else:
            currencies.add(position.lot.currency)

            # Compute the amount to balance and update the inventory.
            weight = get_posting_weight(posting)
            residual.add_amount(weight)

            has_regular_postings = True
            if weight:
                has_nonzero_amount = True

    # If there are auto-postings, fill them in.
    has_inserted = False
    if auto_postings_indices:

        # If there are too many such postings, we can't do anything, barf.
        if len(auto_postings_indices) > 1:
            balance_errors.append(
                BalanceError(entry.meta,
                             "Too many auto-postings; cannot fill in",
                             entry))
            # Delete the redundant auto-postings.
            for index in sorted(auto_postings_indices[1:], reverse=1):
                del postings[index]

        index = auto_postings_indices[0]
        old_posting = postings[index]
        assert old_posting.price is None

        residual_positions = residual.get_positions()

        # If there are no residual positions, we want to still insert a posting
        # but with a zero position for each currency, so that the posting shows
        # up anyhow. We insert one such posting for each currency seen in the
        # complete postings. Note: if all the non-auto postings are zero, we
        # want to avoid sending a warning; the input text clearly implies the
        # author knows this would be useless.
        new_postings = []
        if not residual_positions and ((has_regular_postings and has_nonzero_amount) or
                                       not has_regular_postings):
            balance_errors.append(
                BalanceError(entry.meta,
                             "Useless auto-posting: {}".format(residual), entry))
            for currency in currencies:
                position = Position(Lot(currency, None, None), ZERO)
                meta = copy.copy(old_posting.meta) if old_posting.meta else {}
                meta[AUTOMATIC_META] = True
                new_postings.append(
                    Posting(entry, old_posting.account, position,
                            None, old_posting.flag, old_posting.meta))
                has_inserted = True
        else:
            # Convert all the residual positions in inventory into a posting for
            # each position.
            for position in residual_positions:
                position = -position

                # Applying rounding to the deafult tolerance, if there is one.
                tolerance = inventory.get_tolerance(tolerances,
                                                    default_tolerances,
                                                    position.lot.currency)
                if tolerance:
                    quantum = (tolerance * 2).normalize()
                    position.number = position.number.quantize(quantum)

                meta = copy.copy(old_posting.meta) if old_posting.meta else {}
                meta[AUTOMATIC_META] = True
                new_postings.append(
                    Posting(entry, old_posting.account, position,
                            None, old_posting.flag, meta))
                has_inserted = True

                # Update the residuals inventory.
                weight = position.get_weight(None)
                residual.add_amount(weight)

        postings[index:index+1] = new_postings

    else:
        # Checking for unbalancing transactions has been moved to the validation
        # stage, so although we already have the input transaction's residuals
        # conveniently precomputed here, we are postponing the check to allow
        # plugins to "fixup" unbalancing transactions. We want to allow users to
        # be able to input unbalancing transactions as long as the final
        # transactions objects that appear on the stream (after processing the
        # plugins) are balanced. See {9e6c14b51a59}.
        pass

    return (postings, has_inserted, balance_errors, residual, tolerances)


def balance_incomplete_postings(entry, options_map):
    """Balance an entry with incomplete postings, modifying the
    empty postings on the entry itself. This sets the parent of
    all the postings to this entry. Futhermore, it stores the dict
    of inferred tolerances as metadata.

    WARNING: This destructively modified entry itself!

    Args:
      entry: An instance of a valid directive. This entry is modified by
        having new postings inserted to it.
      options_map: A dict of options, as produced by the parser.
    Returns:
      A list of errors, or None, if none occurred.
    """
    # No postings... nothing to do.
    if not entry.postings:
        return None

    # Get the list of corrected postings.
    (postings, unused_inserted, errors,
     residual, tolerances) = get_incomplete_postings(entry, options_map)

    # If we need to accumulate rounding error to accumulate the residual, add
    # suitable postings here.
    if not residual.is_empty():
        account_rounding = options_map["account_rounding"]
        if account_rounding:
            rounding_postings = get_residual_postings(residual, account_rounding)
            postings.extend(rounding_postings)

    # If we could make this faster to avoid the unnecessary copying, it would
    # make parsing substantially faster.
    # PERF(25ms): could be saved here by avoiding reparenting.
    entry.postings.clear()
    for posting in postings:
        entry.postings.append(reparent_posting(posting, entry))

    if entry.meta is None:
        entry.meta = {}
    entry.meta['__tolerances__'] = tolerances

    return errors or None


def compute_postings_balance(postings):
    """Compute the balance of a list of Postings's positions.

    Args:
      postings: A list of Posting instances and other directives (which are
        skipped).
    Returns:
      An Inventory.
    """
    final_balance = Inventory()
    for posting in postings:
        if isinstance(posting, Posting):
            final_balance.add_position(posting.position)
    return final_balance


def compute_entries_balance(entries, prefix=None, date=None):
    """Compute the balance of all postings of a list of entries.

    Sum up all the positions in all the postings of all the transactions in the
    list of entries and return an inventory of it.

    Args:
      entries: A list of directives.
      prefix: If specified, a prefix string to restrict by account name. Only
        postings with an account that starts with this prefix will be summed up.
      date: A datetime.date instance at which to stop adding up the balance.
        The date is exclusive.
    Returns:
      An instance of Inventory.
    """
    total_balance = Inventory()
    for entry in entries:
        if not (date is None or entry.date < date):
            break
        if isinstance(entry, Transaction):
            for posting in entry.postings:
                if prefix is None or posting.account.startswith(prefix):
                    total_balance.add_position(posting.position)
    return total_balance


def compute_entry_context(entries, context_entry):
    """Compute the balances of all accounts referenced by entry up to entry.

    This provides the inventory of the accounts to which the entry is to be
    applied, before and after.

    Args:
      entries: A list of directives.
      context_entry: The entry for which we want to obtain the before and after
        context.
    Returns:
      Two dicts of account-name to Inventory instance, one which represents the
      context before the entry is applied, and one that represents the context
      after it has been applied.
    """
    assert context_entry is not None, "context_entry is missing."

    # Get the set of accounts for which to compute the context.
    context_accounts = getters.get_entry_accounts(context_entry)

    # Iterate over the entries until we find the target one and accumulate the
    # balance.
    context_before = collections.defaultdict(inventory.Inventory)
    for entry in entries:
        if entry is context_entry:
            break
        if isinstance(entry, Transaction):
            for posting in entry.postings:
                if not any(posting.account == account
                           for account in context_accounts):
                    continue
                balance = context_before[posting.account]
                balance.add_position(posting.position)

    # Compute the after context for the entry.
    context_after = copy.deepcopy(context_before)
    if isinstance(context_entry, Transaction):
        for posting in entry.postings:
            balance = context_after[posting.account]
            balance.add_position(posting.position)

    return context_before, context_after
