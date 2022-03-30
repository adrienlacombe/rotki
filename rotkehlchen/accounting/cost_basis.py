import logging
from collections import defaultdict
from dataclasses import dataclass, field
from typing import (
    TYPE_CHECKING,
    Any,
    Callable,
    DefaultDict,
    Dict,
    List,
    Literal,
    NamedTuple,
    Optional,
    Type,
    overload,
)

from rotkehlchen.accounting.mixins.event import AccountingEventType
from rotkehlchen.accounting.pnl import PNL
from rotkehlchen.assets.asset import Asset
from rotkehlchen.constants import BCH_BSV_FORK_TS, BTC_BCH_FORK_TS, ETH_DAO_FORK_TS
from rotkehlchen.constants.assets import A_BCH, A_BSV, A_BTC, A_ETC, A_ETH, A_WETH
from rotkehlchen.constants.misc import ZERO
from rotkehlchen.db.settings import DBSettings
from rotkehlchen.errors import DeserializationError
from rotkehlchen.fval import FVal
from rotkehlchen.logging import RotkehlchenLogsAdapter
from rotkehlchen.types import Location, Price, Timestamp
from rotkehlchen.user_messages import MessagesAggregator
from rotkehlchen.utils.mixins.customizable_date import CustomizableDateMixin

if TYPE_CHECKING:
    from rotkehlchen.accounting.processed_event import ProcessedAccountingEvent
    from rotkehlchen.db.dbhandler import DBHandler

logger = logging.getLogger(__name__)
log = RotkehlchenLogsAdapter(logger)


@dataclass(init=True, repr=True, eq=True, order=False, unsafe_hash=False, frozen=False)
class AssetAcquisitionEvent:
    event: 'ProcessedAccountingEvent'
    remaining_amount: FVal = field(init=False)  # Same as amount but reduced during processing

    @property
    def amount(self) -> FVal:
        """Amount of the asset being bought"""
        return self.event.taxable_amount

    @property
    def timestamp(self) -> Timestamp:
        return self.event.timestamp

    @property
    def rate(self) -> Price:
        return self.event.price

    def __post_init__(self) -> None:
        self.remaining_amount = self.amount

    def __str__(self) -> str:
        return (
            f'AssetAcquisitionEvent {self.event.notes} in {str(self.event.location)} '
            f'@ {self.event.timestamp}. amount: {self.amount} rate: {self.event.price}'
        )

    def serialize(self) -> Dict[str, Any]:
        """Turn to a dict to be returned by the API and shown in the UI"""
        return {
            'time': self.event.timestamp,
            'description': self.event.notes,
            'location': str(self.event.location),
            'amount': str(self.amount),
            'rate': str(self.event.price),
        }


@dataclass(init=True, repr=True, eq=True, order=False, unsafe_hash=False, frozen=False)
class AssetSpendEvent:
    timestamp: Timestamp
    location: Location
    amount: FVal  # Amount of the asset we sell
    rate: FVal  # Rate in 'profit_currency' for which we sell 1 unit of the sold asset

    def __str__(self) -> str:
        return (
            f'AssetSpendEvent in {str(self.location)} @ {self.timestamp}.'
            f'amount: {self.amount} rate: {self.rate}'
        )


@dataclass(init=True, repr=True, eq=True, order=False, unsafe_hash=False, frozen=False)
class CostBasisEvents:
    used_acquisitions: List[AssetAcquisitionEvent] = field(init=False)
    acquisitions: List[AssetAcquisitionEvent] = field(init=False)
    spends: List[AssetSpendEvent] = field(init=False)

    def __post_init__(self) -> None:
        """Using this since can't use mutable default arguments"""
        self.used_acquisitions = []
        self.acquisitions = []
        self.spends = []


class MatchedAcquisition(NamedTuple):
    amount: FVal
    event: AssetAcquisitionEvent

    def serialize(self) -> Dict[str, Any]:
        """Turn to a dict to be returned by the API and shown in the UI"""
        serialized_acquisition = self.event.serialize()
        serialized_acquisition['used_amount'] = str(self.amount)
        return serialized_acquisition

    def to_string(self, converter: Callable[[Timestamp], str]) -> str:
        return (
            f'{self.amount} / {self.event.amount}  acquired in {str(self.event.event.location)}'
            f' at {converter(self.event.timestamp)} for price: {self.event.event.price}'
        )


class CostBasisInfo(NamedTuple):
    """Information on the cost basis of a spend event

        - `taxable_amount`: The amount out of `spending_amount` that is taxable,
                            calculated from the free after given time period rule.
        - `taxable_bought_cost`: How much it cost in `profit_currency` to buy
                                 the `taxable_amount`
        - `taxfree_bought_cost`: How much it cost in `profit_currency` to buy
                                 the taxfree_amount (selling_amount - taxable_amount)
        - `matched_acquisitions`: The list of acquisitions and amount per acquisition
                                   used for this spend
        - `is_complete: Boolean denoting whether enough information was recovered for the spend
    """
    taxable_amount: FVal
    taxable_bought_cost: FVal
    taxfree_bought_cost: FVal
    matched_acquisitions: List[MatchedAcquisition]
    is_complete: bool

    def serialize(self) -> Dict[str, Any]:
        """Turn to a dict to be returned by the API and shown in the UI"""
        return {
            'is_complete': self.is_complete,
            'matched_acquisitions': [x.serialize() for x in self.matched_acquisitions],
        }

    @classmethod
    def deserialize(cls: Type['CostBasisInfo'], data: Dict[str, Any]) -> Optional['CostBasisInfo']:
        """Creates a CostBasisInfo object from a json dict made from serialize()

        May raise:
        - DeserializationError
        """
        try:
            is_complete = data['is_complete']
            matched_acquisitions = []
            for entry in data['matched_acquisitions']:
                # Here entries are stringified matched acquisitions
                # TODO: This is a hack and a bad one. We have no way to serialize/deserialize
                # matched acquisitions. This is fine here since deserialized CostBasisInfo
                # currently only goes into CSV and api which is consuming it stringified.
                # But we have to fix this
                matched_acquisitions.append(entry)
        except KeyError as e:
            raise DeserializationError(f'Could not decode CostBasisInfo json from the DB due to missing key {str(e)}') from e  # noqa: E501

        return CostBasisInfo(  # the 0 are not serialized and not used at recall so is okay to skip
            taxable_amount=ZERO,
            taxable_bought_cost=ZERO,
            taxfree_bought_cost=ZERO,
            is_complete=is_complete,
            matched_acquisitions=matched_acquisitions,
        )

    def to_string(self, converter: Callable[[Timestamp], str]) -> str:
        """Turn to a string to be shown in exported files such as CSV"""
        value = ''
        if not self.is_complete:
            value += 'Incomplete cost basis information for spend. '

        if len(self.matched_acquisitions) == 0:
            return value

        value += f'Used: {"|".join([x.to_string(converter) for x in self.matched_acquisitions])}'
        return value


class CostBasisCalculator(CustomizableDateMixin):

    def __init__(
            self,
            database: 'DBHandler',
            msg_aggregator: MessagesAggregator,
    ) -> None:
        super().__init__(database=database)
        self._taxfree_after_period: Optional[int] = None
        self.msg_aggregator = msg_aggregator
        self.reset(self.settings)

    def reset(self, settings: DBSettings) -> None:
        self.settings = settings
        self.profit_currency = settings.main_currency
        self._events: DefaultDict[Asset, CostBasisEvents] = defaultdict(CostBasisEvents)

    def get_events(self, asset: Asset) -> CostBasisEvents:
        """Custom getter for events so that we have common cost basis for some assets"""
        if asset == A_WETH:
            asset = A_ETH

        return self._events[asset]

    def inform_user_missing_acquisition(
            self,
            asset: Asset,
            time: Timestamp,
            found_amount: Optional[FVal] = None,
            missing_amount: Optional[FVal] = None,
    ) -> None:
        """Inform the user for missing data for an acquisition via the msg aggregator"""
        if found_amount is None:
            self.msg_aggregator.add_error(
                f'No documented acquisition found for {asset} before '
                f'{self.timestamp_to_date(time)}. Let rotki '
                f'know how you acquired it via a ledger action',
            )
            return

        self.msg_aggregator.add_error(
            f'Not enough documented acquisitions found for {asset} before '
            f'{self.timestamp_to_date(time)}. Only found acquisitions '
            f'for {found_amount} {asset} and miss {missing_amount} {asset}.'
            f'Let rotki know how you acquired it via a ledger action',
        )

    def reduce_asset_amount(self, asset: Asset, amount: FVal, timestamp: Timestamp) -> bool:
        """Searches all acquisition events for asset and reduces them by amount.

        Returns True if enough acquisition events to reduce the asset by amount were
        found and False otherwise.

        In the case of insufficient acquisition amounts a critical error is logged.

        This function does the same as calculate_spend_cost_basis as far as consuming
        acquisitions is concerned but does not calculate bought cost.
        """
        # No need to do anything if amount is to be reduced by zero
        if amount == ZERO:
            return True

        asset_events = self.get_events(asset)
        if len(asset_events.acquisitions) == 0:
            return False

        remaining_amount_from_last_buy = FVal('-1')
        remaining_amount = amount
        for idx, acquisition_event in enumerate(asset_events.acquisitions):
            if remaining_amount < acquisition_event.remaining_amount:
                stop_index = idx
                remaining_amount_from_last_buy = acquisition_event.remaining_amount - remaining_amount  # noqa: E501
                # stop iterating since we found all acquisitions to satisfy reduction
                break

            # else
            remaining_amount -= acquisition_event.remaining_amount
            if idx == len(asset_events.acquisitions) - 1:
                stop_index = idx + 1

        # Otherwise, delete all the used up acquisitions from the list
        del asset_events.acquisitions[:stop_index]
        # and modify the amount of the buy where we stopped if there is one
        if remaining_amount_from_last_buy != FVal('-1'):
            asset_events.acquisitions[0].remaining_amount = remaining_amount_from_last_buy
        elif remaining_amount != ZERO:
            log.critical(
                f'No documented buy found for {asset} before '
                f'{self.timestamp_to_date(timestamp)}',
            )
            return False

        return True

    def obtain_asset(
            self,
            event: 'ProcessedAccountingEvent',
    ) -> None:
        """Adds an acquisition event for an asset"""
        asset_event = AssetAcquisitionEvent(event=event)
        asset_events = self.get_events(asset_event.event.asset)
        asset_events.acquisitions.append(asset_event)

    @overload
    def spend_asset(
            self,
            location: Location,
            timestamp: Timestamp,
            asset: Asset,
            amount: FVal,
            rate: FVal,
            taxable_spend: Literal[True],
    ) -> CostBasisInfo:
        ...

    @overload
    def spend_asset(
            self,
            location: Location,
            timestamp: Timestamp,
            asset: Asset,
            amount: FVal,
            rate: FVal,
            taxable_spend: Literal[False],
    ) -> None:
        ...

    @overload  # not sure why we need this overload too -> https://github.com/python/mypy/issues/6113  # noqa: E501
    def spend_asset(
            self,
            location: Location,
            timestamp: Timestamp,
            asset: Asset,
            amount: FVal,
            rate: FVal,
            taxable_spend: bool,
    ) -> Optional[CostBasisInfo]:
        ...

    def spend_asset(
            self,
            location: Location,
            timestamp: Timestamp,
            asset: Asset,
            amount: FVal,
            rate: FVal,
            taxable_spend: bool,
    ) -> Optional[CostBasisInfo]:
        """
        Register an asset spending event. For example from a trade, a fee, a swap.

        The `taxable_spend` argument defines if this spend is to be considered taxable or not.
        This is important for customization of accounting for some events such as swapping
        ETH for aETH, locking GNO for LockedGNO. In many jurisdictions in this case
        it can be considered as locking/depositing instead of swapping.
        """
        event = AssetSpendEvent(
            location=location,
            timestamp=timestamp,
            amount=amount,
            rate=rate,
        )
        asset_events = self.get_events(asset)
        asset_events.spends.append(event)
        if not asset.is_fiat() and taxable_spend:
            return self.calculate_spend_cost_basis(
                spending_amount=amount,
                spending_asset=asset,
                timestamp=timestamp,
            )
        # else just reduce the amount's acquisition without counting anything
        self.reduce_asset_amount(asset=asset, amount=amount, timestamp=timestamp)
        return None

    def calculate_spend_cost_basis(
            self,
            spending_amount: FVal,
            spending_asset: Asset,
            timestamp: Timestamp,
    ) -> CostBasisInfo:
        """
        When spending `spending_amount` of `spending_asset` at `timestamp` this function
        calculates using the first-in-first-out rule the corresponding buy/s from
        which to do profit calculation. Also applies the "free after given time period"
        rule which applies for some jurisdictions such as 1 year for Germany.

        Returns the information in a CostBasisInfo object if enough acquisitions have
        been found.
        """
        remaining_sold_amount = spending_amount
        stop_index = -1
        taxfree_bought_cost = taxable_bought_cost = taxable_amount = taxfree_amount = ZERO  # noqa: E501
        remaining_amount_from_last_buy = FVal('-1')
        matched_acquisitions = []
        asset_events = self.get_events(spending_asset)

        for idx, acquisition_event in enumerate(asset_events.acquisitions):
            if self.settings.taxfree_after_period is None:
                at_taxfree_period = False
            else:
                at_taxfree_period = (
                    acquisition_event.timestamp + self.settings.taxfree_after_period < timestamp
                )

            if remaining_sold_amount < acquisition_event.remaining_amount:
                stop_index = idx
                acquisition_cost = acquisition_event.rate * remaining_sold_amount

                if at_taxfree_period:
                    taxfree_amount += remaining_sold_amount
                    taxfree_bought_cost += acquisition_cost
                else:
                    taxable_amount += remaining_sold_amount
                    taxable_bought_cost += acquisition_cost

                remaining_amount_from_last_buy = acquisition_event.remaining_amount - remaining_sold_amount  # noqa: E501
                log.debug(
                    'Spend uses up part of historical acquisition',
                    tax_status='TAX-FREE' if at_taxfree_period else 'TAXABLE',
                    used_amount=remaining_sold_amount,
                    from_amount=acquisition_event.amount,
                    asset=spending_asset,
                    acquisition_rate=acquisition_event.rate,
                    profit_currency=self.profit_currency,
                    time=self.timestamp_to_date(acquisition_event.timestamp),
                )
                matched_acquisitions.append(MatchedAcquisition(
                    amount=remaining_sold_amount,
                    event=acquisition_event,
                ))
                # stop iterating since we found all acquisitions to satisfy this spend
                break

            remaining_sold_amount -= acquisition_event.remaining_amount
            acquisition_cost = acquisition_event.rate * acquisition_event.remaining_amount
            if at_taxfree_period:
                taxfree_amount += acquisition_event.remaining_amount
                taxfree_bought_cost += acquisition_cost
            else:
                taxable_amount += acquisition_event.remaining_amount
                taxable_bought_cost += acquisition_cost

            log.debug(
                'Spend uses up entire historical acquisition',
                tax_status='TAX-FREE' if at_taxfree_period else 'TAXABLE',
                bought_amount=acquisition_event.remaining_amount,
                asset=spending_asset,
                acquisition_rate=acquisition_event.rate,
                profit_currency=self.profit_currency,
                time=self.timestamp_to_date(acquisition_event.timestamp),
            )
            matched_acquisitions.append(MatchedAcquisition(
                amount=acquisition_event.remaining_amount,
                event=acquisition_event,
            ))
            # and since this events is going to be removed, reduce its remaining to zero
            acquisition_event.remaining_amount = ZERO

            # If the sell used up the last historical acquisition
            if idx == len(asset_events.acquisitions) - 1:
                stop_index = idx + 1

        if len(asset_events.acquisitions) == 0:
            self.inform_user_missing_acquisition(spending_asset, timestamp)
            # That means we had no documented acquisition for that asset. This is not good
            # because we can't prove a corresponding acquisition and as such we are burdened
            # calculating the entire spend as profit which needs to be taxed
            return CostBasisInfo(
                taxable_amount=spending_amount,
                taxable_bought_cost=ZERO,
                taxfree_bought_cost=ZERO,
                matched_acquisitions=[],
                is_complete=False,
            )

        is_complete = True
        # Otherwise, delete all the used up acquisitions from the list
        asset_events.used_acquisitions.extend(
            asset_events.acquisitions[:stop_index],
        )
        del asset_events.acquisitions[:stop_index]
        # and modify the amount of the buy where we stopped if there is one
        if remaining_amount_from_last_buy != FVal('-1'):
            asset_events.acquisitions[0].remaining_amount = remaining_amount_from_last_buy  # noqa: E501
        elif remaining_sold_amount != ZERO:
            # if we still have sold amount but no acquisitions to satisfy it then we only
            # found acquisitions to partially satisfy the sell
            adjusted_amount = spending_amount - taxfree_amount
            self.inform_user_missing_acquisition(
                asset=spending_asset,
                time=timestamp,
                found_amount=taxable_amount + taxfree_amount,
                missing_amount=remaining_sold_amount,
            )
            taxable_amount = adjusted_amount
            is_complete = False

        return CostBasisInfo(
            taxable_amount=taxable_amount,
            taxable_bought_cost=taxable_bought_cost,
            taxfree_bought_cost=taxfree_bought_cost,
            matched_acquisitions=matched_acquisitions,
            is_complete=is_complete,
        )

    def get_calculated_asset_amount(self, asset: Asset) -> Optional[FVal]:
        """Get the amount of asset accounting has calculated we should have after
        the history has been processed
        """
        asset_events = self.get_events(asset)
        if len(asset_events.acquisitions) == 0:
            return None

        amount = FVal(0)
        for acquisition_event in asset_events.acquisitions:
            amount += acquisition_event.remaining_amount
        return amount

    def handle_prefork_asset_acquisitions(
            self,
            location: Location,
            timestamp: Timestamp,
            asset: Asset,
            amount: FVal,
            price: Price,
    ) -> List['ProcessedAccountingEvent']:
        """
        Calculate the prefork asset acquisitions, meaning how is the acquisition
        of ETC pre ETH fork handled etc.

        TODO: This should change for https://github.com/rotki/rotki/issues/1610

        Returns the acquisition events to append to the pot
        """
        # TODO: Fix this circular dependency with ProcessedAccountingEvent
        from rotkehlchen.accounting.processed_event import ProcessedAccountingEvent
        acquisitions = []
        if asset == A_ETH and timestamp < ETH_DAO_FORK_TS:
            acquisitions = [(A_ETC, 'Prefork acquisition for ETC')]
        elif asset == A_BTC and timestamp < BTC_BCH_FORK_TS:
            # Acquiring BTC before the BCH fork provides equal amount of BCH and BSV
            acquisitions = [
                (A_BCH, 'Prefork acquisition for BCH'),
                (A_BSV, 'Prefork acquisition for BSV'),
            ]
        elif asset == A_BCH and timestamp < BCH_BSV_FORK_TS:
            # Acquiring BCH before the BSV fork provides equal amount of BSV
            acquisitions = [(A_BSV, 'Prefork acquisition for BSV')]

        events = []
        for acquisition in acquisitions:
            event = ProcessedAccountingEvent(
                type=AccountingEventType.PREFORK_ACQUISITION,
                notes=acquisition[1],
                location=location,
                timestamp=timestamp,
                asset=acquisition[0],
                taxable_amount=amount,
                free_amount=ZERO,
                price=price,
                pnl=PNL(),
                cost_basis=None,
            )
            self.obtain_asset(event)
            events.append(event)

        return events

    def handle_prefork_asset_spends(
            self,
            asset: Asset,
            amount: FVal,
            timestamp: Timestamp,
    ) -> None:
        """
        Calculate the prefork asset spends, meaning the opposite of
        handle_prefork_asset_acquisitions

        TODO: This should change for https://github.com/rotki/rotki/issues/1610
        """
        # For now for those don't use inform_user_missing_acquisition since if those hit
        # the preforked asset acquisition data is what's missing so user would getLogger
        # two messages. So as an example one for missing ETH data and one for ETC data
        if asset == A_ETH and timestamp < ETH_DAO_FORK_TS:
            self.reduce_asset_amount(asset=A_ETC, amount=amount, timestamp=timestamp)

        if asset == A_BTC and timestamp < BTC_BCH_FORK_TS:
            self.reduce_asset_amount(asset=A_BCH, amount=amount, timestamp=timestamp)
            self.reduce_asset_amount(asset=A_BSV, amount=amount, timestamp=timestamp)

        if asset == A_BCH and timestamp < BCH_BSV_FORK_TS:
            self.reduce_asset_amount(asset=A_BSV, amount=amount, timestamp=timestamp)
