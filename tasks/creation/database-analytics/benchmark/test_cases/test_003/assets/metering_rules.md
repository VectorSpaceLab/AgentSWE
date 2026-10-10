# Charging metering and tariff rules

Assign a session to local service date by shifting `sessions.started_at_utc` by that row's `utc_offset_minutes_at_start`. The per-session offset is authoritative and already captures offset transitions. Do not use `sites.current_offset_minutes` or the raw UTC date.

Include only `session_status = 'completed'`. For each `meter_reading_versions.reading_key`, retain the greatest revision. For retained `reading_kind = 'import'` rows, the start endpoint is the reading with the smallest `reading_seq` and the end endpoint is the reading with the greatest `reading_seq`; delivered Wh are end `meter_wh` minus start `meter_wh`. Do not sum cumulative registers, substitute numeric minimum/maximum meter values, or include `export` readings. A retained negative endpoint delta is an integrity blocker and must not be absolute-valued; stale negative lower revisions are not retained evidence. Ignore `estimated_kwh`.

For each `tariff_versions.tariff_key`, retain the greatest revision. At the session's local start timestamp, select the one retained row for its site whose half-open interval begins at `effective_local` and ends at `expires_local` or is open-ended. `energy_price_cents_per_kwh` is cents per kWh. Energy cost is `delivered_wh / 1000 * cents_per_kwh / 100`.

Aggregate without early rounding. Display kWh to three decimals and USD to two decimals. If unrounded daily energy ties for a site's peak, select the earlier local date.

All data is synthetic.
