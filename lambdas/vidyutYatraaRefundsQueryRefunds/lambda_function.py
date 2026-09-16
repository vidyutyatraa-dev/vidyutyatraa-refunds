import json
import os
from decimal import Decimal

import boto3
from boto3.dynamodb.conditions import Key, Attr
from botocore.exceptions import ClientError


# ============================================================
# CONFIGURATION
# ============================================================

REFUNDS_TABLE_NAME = os.environ.get(
    "REFUNDS_TABLE_NAME",
    "VidyutYatraaRefunds"
)

EVUSERS_TABLE_NAME = os.environ.get(
    "EVUSERS_TABLE_NAME",
    "VidyutYatraaEVUsers"
)

EMAIL_INDEX = "email-filedOn-index"
STATUS_INDEX = "refundStatus-filedOn-index"
AMOUNT_INDEX = "refundAmount-index"

DEFAULT_STATUSES = ["Initiated", "Approved"]

VALID_STATUSES = {
    "Initiated",
    "Approved",
    "Settled",
    "Rejected",
}

dynamodb = boto3.resource("dynamodb")
refunds = dynamodb.Table(REFUNDS_TABLE_NAME)
users = dynamodb.Table(EVUSERS_TABLE_NAME)

# ============================================================
# RESPONSE HELPERS
# ============================================================

def decimal_to_native(value):
    if isinstance(value, Decimal):
        if value % 1 == 0:
            return int(value)
        return float(value)

    if isinstance(value, dict):
        return {
            key: decimal_to_native(val)
            for key, val in value.items()
        }

    if isinstance(value, list):
        return [
            decimal_to_native(val)
            for val in value
        ]

    return value


def response(status_code, body):
    return {
        "statusCode": status_code,
        "headers": {
            "Content-Type": "application/json",
            "Access-Control-Allow-Origin": "*",
            "Access-Control-Allow-Headers": "Content-Type,Authorization",
            "Access-Control-Allow-Methods": "GET,OPTIONS",
        },
        "body": json.dumps(decimal_to_native(body)),
    }


# ============================================================
# QUERY PARAMETER HELPERS
# ============================================================

def get_query_params(event):
    return event.get("queryStringParameters") or {}


def get_multi_query_params(event, name):
    """
    Supports:

        ?refundStatus=Initiated&refundStatus=Approved

    and:

        ?refundStatus=Initiated,Approved
    """

    multi_params = (
        event.get("multiValueQueryStringParameters")
        or {}
    )

    values = multi_params.get(name)

    if values:
        result = []

        for value in values:
            if value is None:
                continue

            result.extend(
                part.strip()
                for part in value.split(",")
                if part.strip()
            )

        return result

    params = get_query_params(event)
    value = params.get(name)

    if not value:
        return []

    return [
        part.strip()
        for part in value.split(",")
        if part.strip()
    ]


def parse_number(value, parameter_name):
    if value is None or value == "":
        return None

    try:
        return Decimal(str(value))
    except Exception:
        raise ValueError(
            f"Invalid numeric value for {parameter_name}"
        )


def get_filed_on_range(params):
    filed_on_from = parse_number(
        params.get("filedOnFrom"),
        "filedOnFrom",
    )

    filed_on_to = parse_number(
        params.get("filedOnTo"),
        "filedOnTo",
    )

    if (
        filed_on_from is not None
        and filed_on_to is not None
        and filed_on_from > filed_on_to
    ):
        raise ValueError(
            "filedOnFrom cannot be greater than filedOnTo"
        )

    return filed_on_from, filed_on_to


# ============================================================
# FILTER EXPRESSION
# ============================================================

def build_filter_expression(
    params,
    status_values,
    used_key_filter=None,
):
    """
    Build filters for attributes that are NOT being used as
    DynamoDB Query key conditions.

    Important:
        Attr() does NOT select another GSI.
        Only KeyConditionExpression determines the selected
        DynamoDB index access path.
    """

    used_key_filter = used_key_filter or {}
    filters = []

    # --------------------------------------------------------
    # EMAIL
    # --------------------------------------------------------

    email = (
        params.get("email")
        or ""
    ).strip()

    if email and not used_key_filter.get("email"):
        filters.append(
            Attr("email").eq(email)
        )

    # --------------------------------------------------------
    # REFUND ID
    # --------------------------------------------------------

    refund_id = (
        params.get("refundId")
        or ""
    ).strip()

    if refund_id and not used_key_filter.get("refundId"):
        filters.append(
            Attr("refundId").eq(refund_id)
        )

    # --------------------------------------------------------
    # REFUND AMOUNT
    # --------------------------------------------------------

    amount_operator = (
        params.get("amountOperator")
        or ""
    ).strip().lower()

    amount_value_1 = parse_number(
        params.get("amountValue1"),
        "amountValue1",
    )

    amount_value_2 = parse_number(
        params.get("amountValue2"),
        "amountValue2",
    )

    if amount_value_1 is not None:

        if amount_operator == "equal":
            if not used_key_filter.get("refundAmount"):
                filters.append(
                    Attr("refundAmount").eq(amount_value_1)
                )

        elif amount_operator == "greater":
            filters.append(
                Attr("refundAmount").gt(amount_value_1)
            )

        elif amount_operator == "less":
            filters.append(
                Attr("refundAmount").lt(amount_value_1)
            )

        elif amount_operator == "between":
            if amount_value_2 is None:
                raise ValueError(
                    "amountValue2 is required for between"
                )

            if amount_value_1 > amount_value_2:
                raise ValueError(
                    "amountValue1 cannot be greater than amountValue2"
                )

            filters.append(
                Attr("refundAmount").between(
                    amount_value_1,
                    amount_value_2,
                )
            )

        else:
            raise ValueError(
                "Invalid amountOperator. "
                "Use equal, greater, less or between."
            )

    # --------------------------------------------------------
    # DESCRIPTION KEYWORD SEARCH
    # --------------------------------------------------------
    #
    # Searches BOTH:
    #   refundDetails.refundDescription
    #   refundDetails.refundType
    #
    # DynamoDB contains() is case-sensitive.
    # --------------------------------------------------------

    description = (
        params.get("description")
        or ""
    ).strip()

    if description:
        keyword_filter = (
            Attr(
                "refundDetails.refundDescription"
            ).contains(description)
            |
            Attr(
                "refundDetails.refundType"
            ).contains(description)
        )

        filters.append(keyword_filter)

    # --------------------------------------------------------
    # REFUND STATUS
    # --------------------------------------------------------

    if (
        status_values
        and not used_key_filter.get("refundStatus")
    ):
        status_filter = None

        for status in status_values:
            condition = Attr("refundStatus").eq(status)

            if status_filter is None:
                status_filter = condition
            else:
                status_filter = status_filter | condition

        if status_filter is not None:
            filters.append(status_filter)

    # --------------------------------------------------------
    # COMBINE
    # --------------------------------------------------------

    if not filters:
        return None

    expression = filters[0]

    for condition in filters[1:]:
        expression = expression & condition

    return expression


# ============================================================
# KEY CONDITION HELPERS
# ============================================================

def build_partition_sort_key_condition(
    partition_key,
    partition_value,
    filed_on_from=None,
    filed_on_to=None,
):
    """
    Builds a DynamoDB Query KeyConditionExpression.

    filedOn is a SORT KEY on:
        email-filedOn-index
        refundStatus-filedOn-index
    """

    condition = Key(partition_key).eq(partition_value)

    if (
        filed_on_from is not None
        and filed_on_to is not None
    ):
        condition = (
            condition
            & Key("filedOn").between(
                filed_on_from,
                filed_on_to,
            )
        )

    elif filed_on_from is not None:
        condition = (
            condition
            & Key("filedOn").gte(
                filed_on_from
            )
        )

    elif filed_on_to is not None:
        condition = (
            condition
            & Key("filedOn").lte(
                filed_on_to
            )
        )

    return condition


# ============================================================
# QUERY EXECUTION
# ============================================================

def execute_query(
    index_name,
    key_condition,
    filter_expression=None,
):
    """
    Execute a fully paginated DynamoDB Query.
    """

    items = []

    kwargs = {
        "IndexName": index_name,
        "KeyConditionExpression": key_condition,
    }

    if filter_expression is not None:
        kwargs["FilterExpression"] = filter_expression

    while True:
        result = refunds.query(**kwargs)

        items.extend(
            result.get("Items", [])
        )

        last_key = result.get(
            "LastEvaluatedKey"
        )

        if not last_key:
            break

        kwargs["ExclusiveStartKey"] = last_key

    return items


def execute_scan(filter_expression=None):
    """
    Fully paginated fallback Scan.

    This should only be reached when no useful GSI
    access path is available.
    """

    items = []

    kwargs = {}

    if filter_expression is not None:
        kwargs["FilterExpression"] = filter_expression

    while True:
        result = refunds.scan(**kwargs)

        items.extend(
            result.get("Items", [])
        )

        last_key = result.get(
            "LastEvaluatedKey"
        )

        if not last_key:
            break

        kwargs["ExclusiveStartKey"] = last_key

    return items


# ============================================================
# REFUND ID LOOKUP
# ============================================================

def get_by_refund_id(refund_id):
    """
    refundId is assumed to be the base refunds partition key.

    GetItem is therefore the highest-performance lookup when
    refundId is supplied.
    """

    result = refunds.get_item(
        Key={
            "refundId": refund_id
        }
    )

    item = result.get("Item")

    if item is None:
        return []

    return [item]


# ============================================================
# QUERY BY STATUS
# ============================================================

def query_by_status(
    statuses,
    filed_on_from,
    filed_on_to,
    filter_expression=None,
):
    """
    Query refundStatus-filedOn-index once per requested status.

    Example:

        Initiated + Approved

    becomes two DynamoDB Query operations:

        PK = Initiated
        SK BETWEEN filed_on_from AND filed_on_to

        PK = Approved
        SK BETWEEN filed_on_from AND filed_on_to
    """

    items = []

    for status in statuses:

        key_condition = (
            build_partition_sort_key_condition(
                "refundStatus",
                status,
                filed_on_from,
                filed_on_to,
            )
        )

        query_items = execute_query(
            STATUS_INDEX,
            key_condition,
            filter_expression,
        )

        items.extend(query_items)

    return items


# ============================================================
# QUERY STRATEGY
# ============================================================

def select_query_strategy(
    params,
    status_values,
    filed_on_from,
    filed_on_to,
):
    """
    Select the best DynamoDB access path.

    Priority:

        1. refundId       -> base table GetItem
        2. email          -> email-filedOn-index
        3. refundStatus   -> refundStatus-filedOn-index
        4. refundAmount equality -> refundAmount-index
        5. Scan fallback

    filedOn is deliberately NOT selected as an independent
    index because it is now a SORT KEY on the composite GSIs.
    """

    refund_id = (
        params.get("refundId")
        or ""
    ).strip()

    email = (
        params.get("email")
        or ""
    ).strip()

    amount_operator = (
        params.get("amountOperator")
        or ""
    ).strip().lower()

    amount_value_1 = parse_number(
        params.get("amountValue1"),
        "amountValue1",
    )

    # --------------------------------------------------------
    # 1. REFUND ID
    # --------------------------------------------------------

    if refund_id:
        return {
            "type": "get_item",
            "index_name": None,
            "key_condition": None,
            "used_filter": {
                "refundId": True,
            },
        }

    # --------------------------------------------------------
    # 2. EMAIL + filedOn range
    # --------------------------------------------------------

    if email:
        key_condition = (
            build_partition_sort_key_condition(
                "email",
                email,
                filed_on_from,
                filed_on_to,
            )
        )

        return {
            "type": "query",
            "index_name": EMAIL_INDEX,
            "key_condition": key_condition,
            "used_filter": {
                "email": True,
            },
        }

    # --------------------------------------------------------
    # 3. STATUS + filedOn range
    # --------------------------------------------------------

    if status_values:
        return {
            "type": "status_query",
            "index_name": STATUS_INDEX,
            "key_condition": None,
            "used_filter": {
                "refundStatus": True,
            },
        }

    # --------------------------------------------------------
    # 4. REFUND AMOUNT equality
    # --------------------------------------------------------

    if (
        amount_operator == "equal"
        and amount_value_1 is not None
    ):
        return {
            "type": "query",
            "index_name": AMOUNT_INDEX,
            "key_condition": Key(
                "refundAmount"
            ).eq(amount_value_1),
            "used_filter": {
                "refundAmount": True,
            },
        }

    # --------------------------------------------------------
    # 5. FALLBACK SCAN
    # --------------------------------------------------------

    return {
        "type": "scan",
        "index_name": None,
        "key_condition": None,
        "used_filter": {},
    }


# ============================================================
# SORT
# ============================================================

def sort_results(items):
    """
    Return dashboard results ordered by filedOn ascending.
    """

    return sorted(
        items,
        key=lambda item: int(
            item.get("filedOn", 0)
        ),
    )

# ============================================================
# REFUND ELIGIBILITY
# ============================================================

def add_refund_allowed_flag(items):
    """
    For every Initiated refund, look up the corresponding user
    in VidyutYatraaEVUsers using email and compare walletBalance
    with refundAmount.

    refundAllowed is added only for Initiated refunds.

    True  -> walletBalance >= refundAmount
    False -> walletBalance < refundAmount

    Refunds with any other status do not receive refundAllowed.
    """

    for item in items:

        if item.get("refundStatus") != "Initiated":
            continue

        email = (
            item.get("email")
            or ""
        ).strip()

        refund_amount = Decimal(
            str(item.get("refundAmount", 0))
        )

        # ----------------------------------------------------
        # No email -> cannot establish wallet balance
        # ----------------------------------------------------
        if not email:
            item["refundAllowed"] = False
            continue

        # ----------------------------------------------------
        # Look up user by email
        #
        # Assumes "email" is the partition key of
        # VidyutYatraaEVUsers.
        # ----------------------------------------------------
        result = users.get_item(
            Key={
                "email": email
            }
        )

        user = result.get("Item")

        if user is None:
            item["refundAllowed"] = False
            continue

        # ----------------------------------------------------
        # Get wallet balance
        # ----------------------------------------------------
        wallet_balance = Decimal(
            str(user.get("walletBalance", 0))
        )

        # ----------------------------------------------------
        # Determine eligibility
        # ----------------------------------------------------
        item["refundAllowed"] = (
            wallet_balance >= refund_amount
        )

    return items

# ============================================================
# MAIN LAMBDA
# ============================================================

def lambda_handler(event, context):

    try:

        # ----------------------------------------------------
        # CORS preflight
        # ----------------------------------------------------

        method = (
            event.get("requestContext", {})
            .get("http", {})
            .get("method")
        )

        if method == "OPTIONS":
            return response(
                200,
                {"message": "OK"},
            )

        # ----------------------------------------------------
        # PARAMETERS
        # ----------------------------------------------------

        params = get_query_params(event)

        status_values = get_multi_query_params(
            event,
            "refundStatus",
        )

        # Default dashboard query:
        # Initiated + Approved
        if not status_values:
            status_values = list(
                DEFAULT_STATUSES
            )

        # Remove duplicates while preserving order.
        status_values = list(
            dict.fromkeys(status_values)
        )

        # ----------------------------------------------------
        # VALIDATE STATUS
        # ----------------------------------------------------

        invalid_statuses = [
            status
            for status in status_values
            if status not in VALID_STATUSES
        ]

        if invalid_statuses:
            return response(
                400,
                {
                    "error": "Invalid refundStatus",
                    "invalidStatuses": invalid_statuses,
                },
            )

        # ----------------------------------------------------
        # PARSE DATE RANGE
        # ----------------------------------------------------

        filed_on_from, filed_on_to = (
            get_filed_on_range(params)
        )

        # ----------------------------------------------------
        # SELECT ACCESS PATH
        # ----------------------------------------------------

        strategy = select_query_strategy(
            params,
            status_values,
            filed_on_from,
            filed_on_to,
        )

        used_filter = strategy.get(
            "used_filter",
            {},
        )

        # ----------------------------------------------------
        # BUILD REMAINING FILTERS
        # ----------------------------------------------------

        filter_expression = (
            build_filter_expression(
                params,
                status_values,
                used_filter,
            )
        )

        # ----------------------------------------------------
        # EXECUTE
        # ----------------------------------------------------

        strategy_type = strategy["type"]

        # ----------------------------------------------------
        # REFUND ID -> GetItem
        # ----------------------------------------------------

        if strategy_type == "get_item":

            refund_id = (
                params.get("refundId")
                or ""
            ).strip()

            items = get_by_refund_id(
                refund_id
            )

            # GetItem cannot take a FilterExpression.
            # Apply all remaining filters in Python.
            if items and filter_expression is not None:
                items = filter_items_in_python(
                    items,
                    params,
                    status_values,
                )

        # ----------------------------------------------------
        # STATUS -> one Query per status
        # ----------------------------------------------------

        elif strategy_type == "status_query":

            items = query_by_status(
                status_values,
                filed_on_from,
                filed_on_to,
                filter_expression,
            )

        # ----------------------------------------------------
        # SINGLE GSI QUERY
        # ----------------------------------------------------

        elif strategy_type == "query":

            items = execute_query(
                strategy["index_name"],
                strategy["key_condition"],
                filter_expression,
            )

        # ----------------------------------------------------
        # FALLBACK SCAN
        # ----------------------------------------------------

        else:

            items = execute_scan(
                filter_expression
            )

        # ----------------------------------------------------
        # REMOVE DUPLICATES
        # ----------------------------------------------------

        unique_items = {}

        for item in items:

            refund_id = item.get(
                "refundId"
            )

            if refund_id:
                unique_items[
                    refund_id
                ] = item

        items = list(
            unique_items.values()
        )

        # ----------------------------------------------------
        # SORT
        # ----------------------------------------------------

        items = sort_results(items)

        items = add_refund_allowed_flag(items)

        # ----------------------------------------------------
        # RESPONSE
        # ----------------------------------------------------

        return response(
            200,
            {
                "refunds": items,
                "count": len(items),
            },
        )

    except ValueError as exc:

        return response(
            400,
            {
                "error": str(exc)
            },
        )

    except ClientError as exc:

        print(
            "DynamoDB ClientError:",
            exc,
        )

        return response(
            500,
            {
                "error": "DynamoDB query failed"
            },
        )

    except Exception as exc:

        print(
            "Unexpected error:",
            exc,
        )

        return response(
            500,
            {
                "error": "Internal server error"
            },
        )


# ============================================================
# PYTHON FILTERING FOR GetItem
# ============================================================

def item_matches_filters(
    item,
    params,
    status_values,
):
    """
    Used only after GetItem(refundId).

    GetItem cannot use FilterExpression, so the remaining
    dashboard filters are evaluated here.
    """

    # --------------------------------------------------------
    # EMAIL
    # --------------------------------------------------------

    email = (
        params.get("email")
        or ""
    ).strip()

    if email and item.get("email") != email:
        return False

    # --------------------------------------------------------
    # STATUS
    # --------------------------------------------------------

    if status_values:
        if item.get("refundStatus") not in status_values:
            return False

    # --------------------------------------------------------
    # FILED ON
    # --------------------------------------------------------

    filed_on = item.get("filedOn")

    if filed_on is None:
        return False

    filed_on = Decimal(str(filed_on))

    filed_on_from, filed_on_to = (
        get_filed_on_range(params)
    )

    if (
        filed_on_from is not None
        and filed_on < filed_on_from
    ):
        return False

    if (
        filed_on_to is not None
        and filed_on > filed_on_to
    ):
        return False

    # --------------------------------------------------------
    # REFUND AMOUNT
    # --------------------------------------------------------

    amount_operator = (
        params.get("amountOperator")
        or ""
    ).strip().lower()

    amount_value_1 = parse_number(
        params.get("amountValue1"),
        "amountValue1",
    )

    amount_value_2 = parse_number(
        params.get("amountValue2"),
        "amountValue2",
    )

    if amount_value_1 is not None:

        amount = Decimal(
            str(item.get("refundAmount", 0))
        )

        if amount_operator == "equal":
            if amount != amount_value_1:
                return False

        elif amount_operator == "greater":
            if amount <= amount_value_1:
                return False

        elif amount_operator == "less":
            if amount >= amount_value_1:
                return False

        elif amount_operator == "between":

            if amount_value_2 is None:
                raise ValueError(
                    "amountValue2 is required for between"
                )

            if (
                amount < amount_value_1
                or amount > amount_value_2
            ):
                return False

    # --------------------------------------------------------
    # DESCRIPTION / REFUND TYPE
    # --------------------------------------------------------

    description = (
        params.get("description")
        or ""
    ).strip()

    if description:

        refund_details = (
            item.get("refundDetails")
            or {}
        )

        refund_description = str(
            refund_details.get(
                "refundDescription",
                "",
            )
        )

        refund_type = str(
            refund_details.get(
                "refundType",
                "",
            )
        )

        if (
            description not in refund_description
            and description not in refund_type
        ):
            return False

    return True


def filter_items_in_python(
    items,
    params,
    status_values,
):
    return [
        item
        for item in items
        if item_matches_filters(
            item,
            params,
            status_values,
        )
    ]
