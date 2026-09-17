# nfl-odds

Daily snapshot of every US sportsbook's NFL spreads, totals and moneylines
(The Odds API), taken by GitHub Actions at 14:00 UTC and appended to
`data_cache/odds_snapshots.csv`.

Columns: snapshot_utc, kickoff_utc, home, away, game (AWAY@HOME), book,
market (spreads / totals / h2h), side, point, price.

The API key lives in the repository secret `ODDS_API_KEY` (Settings ->
Secrets and variables -> Actions). The same script also runs on the Mac
(`~/NFL_Odds`); the two stores are merged by the NFL model scripts.
