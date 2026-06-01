/*
 Copyright (c) 2024-2025 Dell Inc. or its subsidiaries. All Rights Reserved.

 Licensed under the Apache License, Version 2.0 (the "License");
 you may not use this file except in compliance with the License.
 You may obtain a copy of the License at

     http://www.apache.org/licenses/LICENSE-2.0

 Unless required by applicable law or agreed to in writing, software
 distributed under the License is distributed on an "AS IS" BASIS,
 WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
 See the License for the specific language governing permissions and
 limitations under the License.
*/

package generalCollector

import (
	"powerstore-metrics-exporter/collector/client"
	"time"

	"github.com/go-kit/log"
	"github.com/go-kit/log/level"
	"github.com/prometheus/client_golang/prometheus"
	"github.com/tidwall/gjson"
)

var alertSeverities = []string{"Critical", "Major", "Minor"}

type alertCollector struct {
	client    *client.Client
	countDesc *prometheus.Desc
	infoDesc  *prometheus.Desc
	logger    log.Logger
}

func NewAlertCollector(api *client.Client, logger log.Logger) *alertCollector {
	return &alertCollector{
		client: api,
		countDesc: prometheus.NewDesc(
			"powerstore_active_alert_count",
			"Number of active alerts by severity",
			[]string{"severity"},
			prometheus.Labels{"IP": api.IP}),
		infoDesc: prometheus.NewDesc(
			"powerstore_active_alert_info",
			"Active alert details; value is always 1",
			[]string{"alert_id", "severity", "description", "resource_type", "resource_name"},
			prometheus.Labels{"IP": api.IP}),
		logger: logger,
	}
}

func (c *alertCollector) Collect(ch chan<- prometheus.Metric) {
	level.Info(c.logger).Log("msg", "Start collecting alert data")
	startTime := time.Now()

	data, err := c.client.GetAlerts()
	if err != nil {
		level.Warn(c.logger).Log("msg", "get alert data error", "err", err)
		// Emit zero counts so dashboards don't go No Data on a scrape failure
		for _, sev := range alertSeverities {
			ch <- prometheus.MustNewConstMetric(c.countDesc, prometheus.GaugeValue, 0, sev)
		}
		return
	}

	counts := make(map[string]float64, len(alertSeverities))
	for _, sev := range alertSeverities {
		counts[sev] = 0
	}

	for _, alert := range gjson.Parse(data).Array() {
		sev := alert.Get("severity").String()
		if sev == "Info" {
			continue
		}
		if _, ok := counts[sev]; ok {
			counts[sev]++
		}
		ch <- prometheus.MustNewConstMetric(
			c.infoDesc,
			prometheus.GaugeValue,
			1,
			alert.Get("id").String(),
			sev,
			alert.Get("description_l10n").String(),
			alert.Get("resource_type").String(),
			alert.Get("resource_name").String(),
		)
	}

	for _, sev := range alertSeverities {
		ch <- prometheus.MustNewConstMetric(c.countDesc, prometheus.GaugeValue, counts[sev], sev)
	}

	level.Info(c.logger).Log("msg", "Obtaining alerts is successful", "time", time.Since(startTime))
}

func (c *alertCollector) Describe(ch chan<- *prometheus.Desc) {
	ch <- c.countDesc
	ch <- c.infoDesc
}
