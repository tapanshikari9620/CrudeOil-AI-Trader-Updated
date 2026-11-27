
from datetime import date

from data_collector.upstox_read_only import UpstoxReadOnlyService


class OptionChainCollector:
    """
    Collect MCX crude oil option contracts using Upstox option-contract
    metadata.

    Supports:
        - CRUDEOIL  (Regular)
        - CRUDEOILM (Mini)

    Product isolation is strict. Contracts belonging to another product
    are never accepted.
    """

    SUPPORTED_PRODUCTS = {"CRUDEOIL", "CRUDEOILM"}

    def __init__(self, client, repository=None):
        self.client = client
        self.repository = repository

    @staticmethod
    def _validate_product(product: str) -> str:
        product = str(product).upper().strip()

        if product not in OptionChainCollector.SUPPORTED_PRODUCTS:
            raise ValueError(
                f"Unsupported MCX crude product: {product}"
            )

        return product

    def collect_contracts(
        self,
        underlying_key: str,
        expiry_date: str | None = None,
        product: str = "CRUDEOIL",
    ):
        product = self._validate_product(product)

        if not underlying_key:
            raise ValueError(
                "A verified MCX crude underlying instrument key is required"
            )

        # Validate the requested expiry before making the API request.
        if expiry_date:
            try:
                date.fromisoformat(expiry_date)
            except (TypeError, ValueError) as exc:
                raise ValueError(
                    "expiry_date must use YYYY-MM-DD format"
                ) from exc

        today = date.today()

        payload = self.client.option_contracts(
            underlying_key,
            expiry_date,
        )

        if not isinstance(payload, dict):
            raise ValueError(
                "Invalid Upstox option-contract response"
            )

        if payload.get("status") != "success":
            raise ValueError(
                "Upstox option-contract request was not successful"
            )

        rows = payload.get("data")

        if not isinstance(rows, list):
            raise ValueError(
                "Option-contract response data must be a list"
            )

        contracts = []
        seen = set()

        for row in rows:
            if not isinstance(row, dict):
                continue

            # Accept only MCX futures-and-options contracts.
            if row.get("exchange") != "MCX":
                continue

            if row.get("segment") != "MCX_FO":
                continue

            # Accept only call and put options.
            kind = row.get("instrument_type")

            if kind not in {"CE", "PE"}:
                continue

            # Reject contracts explicitly linked to another future.
            returned_underlying_key = row.get("underlying_key")

            if (
                returned_underlying_key
                and returned_underlying_key != underlying_key
            ):
                continue

            # Strict Regular/Mini product isolation.
            returned_product = str(
                row.get("underlying_symbol") or ""
            ).upper().strip()

            if returned_product != product:
                continue

            # Normalize using official Upstox contract metadata.
            item = UpstoxReadOnlyService._normalize_instrument(
                row,
                kind,
                metadata_source="upstox_option_contracts",
            )

            # Recheck product after normalization.
            normalized_product = str(
                item.get("underlying_symbol") or ""
            ).upper().strip()

            if normalized_product != product:
                continue

            # Reject expired contracts.
            contract_expiry = date.fromisoformat(
                str(item["expiry"])
            )

            if contract_expiry < today:
                continue

            # Respect an explicitly requested expiry.
            if (
                expiry_date
                and contract_expiry.isoformat() != expiry_date
            ):
                continue

            # Avoid duplicate instrument records.
            instrument_key = item["instrument_key"]

            if instrument_key in seen:
                continue

            seen.add(instrument_key)

            # Persist only verified, matching contracts.
            if self.repository is not None:
                self.repository.save_verified_instrument(item)

            contracts.append(item)

        return contracts

    def collect(
        self,
        underlying_key: str,
        expiry_date: str | None = None,
        product: str = "CRUDEOIL",
    ):
        return self.collect_contracts(
            underlying_key=underlying_key,
            expiry_date=expiry_date,
            product=product,
        )
