#!/usr/bin/python
# -*- coding:utf-8 -*-
# Filename: exporter.py

import logging
import time
from datetime import datetime, timezone

from azure.core.exceptions import HttpResponseError
from azure.identity import ClientSecretCredential
from azure.mgmt.costmanagement import CostManagementClient
from azure.mgmt.costmanagement.models import QueryDefinition, QueryTimePeriod
from dateutil.relativedelta import relativedelta
from prometheus_client import Gauge


class MetricExporter:
    def __init__(self, polling_interval_seconds, metric_name, metric_name_usd, group_by, targets, secrets):
        self.polling_interval_seconds = polling_interval_seconds
        self.metric_name = metric_name
        self.metric_name_usd = metric_name_usd
        self.group_by = group_by
        self.targets = targets
        self.secrets = secrets
        # we have verified that there is at least one target
        self.labels = set(targets[0].keys())
        # for now we only support exporting one type of cost (ActualCost)
        self.labels.add("ChargeType")
        self.labels.add("Currency")
        if group_by["enabled"]:
            for group in group_by["groups"]:
                self.labels.add(group["label_name"])
        self.azure_daily_cost = Gauge(self.metric_name, "Daily cost of an Azure account in billing currency", self.labels)
        self.azure_daily_cost_usd = Gauge(self.metric_name_usd, "Daily cost of an Azure account in USD", self.labels)

    def run_metrics_loop(self):
        while True:
            # every time we clear up all the existing labels before setting new ones
            self.azure_daily_cost.clear()
            self.azure_daily_cost_usd.clear()

            self.fetch()
            time.sleep(self.polling_interval_seconds)

    def init_azure_client(self, tenant_id):
        client = CostManagementClient(
            credential=ClientSecretCredential(
                tenant_id=tenant_id,
                client_id=self.secrets[tenant_id]["client_id"],
                client_secret=self.secrets[tenant_id]["client_secret"],
            )
        )

        return client

    def query_azure_cost_explorer(self, azure_client, subscription, group_by, start_date, end_date):
        scope = f"/subscriptions/{subscription}"

        groups = list()
        if group_by["enabled"]:
            for group in group_by["groups"]:
                groups.append({"type": group["type"], "name": group["name"]})

        query = QueryDefinition(
            type="ActualCost",
            dataset={
                "granularity": "Daily",
                "aggregation": {
                    "totalCost": {"name": "Cost", "function": "Sum"},
                    "totalCostUSD": {"name": "CostUSD", "function": "Sum"}
                },
                "grouping": groups,
            },
            timeframe="Custom",
            time_period=QueryTimePeriod(
                from_property=datetime(start_date.year, start_date.month, start_date.day, tzinfo=timezone.utc),
                to=datetime(end_date.year, end_date.month, end_date.day, tzinfo=timezone.utc),
            ),
        )
        result = azure_client.query.usage(scope, query)
        return result.as_dict()

    def expose_metrics(self, azure_account, result):   
        cost = float(result[0])
        costUsd = float(result[1])

        if not self.group_by["enabled"]:
            self.azure_daily_cost.labels(**azure_account, ChargeType="ActualCost", Currency=result[3]).set(cost)
            self.azure_daily_cost_usd.labels(**azure_account, ChargeType="ActualCost", Currency=result[3]).set(costUsd)
        else:
            merged_minor_cost = 0
            merged_minor_cost_usd = 0
            group_key_values = dict()
            for i in range(len(self.group_by["groups"])):
                value = result[i + 3]
                group_key_values.update({self.group_by["groups"][i]["label_name"]: value})

            if self.group_by["merge_minor_cost"]["enabled"] and cost < self.group_by["merge_minor_cost"]["threshold"]:
                merged_minor_cost += cost
                merged_minor_cost_usd += costUsd
            else:
                self.azure_daily_cost.labels(**azure_account, **group_key_values, ChargeType="ActualCost", Currency=result[3]).set(cost)
                self.azure_daily_cost_usd.labels(**azure_account, **group_key_values, ChargeType="ActualCost", Currency=result[3]).set(costUsd)

            if merged_minor_cost > 0:
                group_key_values = dict()
                for i in range(len(self.group_by["groups"])):
                    group_key_values.update(
                        {self.group_by["groups"][i]["label_name"]: self.group_by["merge_minor_cost"]["tag_value"]}
                    )
                self.azure_daily_cost.labels(**azure_account, **group_key_values, ChargeType="ActualCost").set(
                    merged_minor_cost
                )
                self.azure_daily_cost_usd.labels(**azure_account, **group_key_values, ChargeType="ActualCost").set(
                    merged_minor_cost_usd
                )

    def fetch(self):
        for azure_account in self.targets:
            print("[%s] Querying cost data for Azure tenant %s" % (datetime.now(), azure_account["TenantId"]))
            azure_client = self.init_azure_client(azure_account["TenantId"])

            try:
                end_date = datetime.today()
                start_date = end_date - relativedelta(days=1)
                cost_response = self.query_azure_cost_explorer(
                    azure_client, azure_account["Subscription"], self.group_by, start_date, end_date
                )
            except HttpResponseError as e:
                logging.error(e.reason)
                continue

            for result in cost_response["rows"]:
                if result[2] != int(start_date.strftime("%Y%m%d")):
                    # it is possible that Azure returns cost data which is different than the specified date
                    # for example, the query time period is 2023-07-10 00:00:00+00:00 to 2023-07-11 00:00:00+00:00
                    # Azure still returns some records for date 2023-07-11
                    continue
                else:
                    self.expose_metrics(azure_account, result)
