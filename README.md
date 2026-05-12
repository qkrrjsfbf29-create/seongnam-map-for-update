# Seongnam Child Allowance Store Data

Public data feed for the iOS app. This repository intentionally contains only public merchant data outputs and update automation.

Generated files:

- `stores.json`: app-ready merchant data
- `stores.csv`: human-readable merchant data with coordinates

Source:

- Dataset: Gyeonggi-do Seongnam-si child allowance merchant status
- Portal: https://www.data.go.kr/data/15129267/fileData.do
- Provider: Gyeonggi-do Seongnam-si
- Original merchant data: Shinhan Card

Secrets are not stored in this repository. The monthly workflow expects these repository secrets:

- `DATA_GO_KR_API_KEY`
- `KAKAO_REST_API_KEY`

The workflow can be run manually from GitHub Actions and is also scheduled monthly.
