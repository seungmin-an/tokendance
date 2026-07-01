<!-- 지식 블록 템플릿. knowledge.md 에 이 형태로 남기면 마스터의 harvest 가 library 로 승격한다.
     아래 한 덩어리가 예시이자 형식이다. 메타(scope/repo/tags/summary)는 모두 선택. -->

## 지식: cargo 테스트는 워크스페이스 루트에서 -p 로 돌린다
scope: repo
repo: foo-service
tags: build, cargo
summary: 하위 크레이트에서 cargo test 하면 워크스페이스 해석 실패

하위 디렉토리에서 `cargo test` 하면 실패한다.
루트에서 `cargo test -p <crate>` 로 돌릴 것.

<!--
파서 계약(예시에서 안 드러나는 것만):
- 헤딩은 "## 지식: <제목>" (H2 + 콜론). 제목이 곧 정체성 → 같은 제목으로 다시 쓰면 멱등 갱신.
- 헤딩 바로 아래 연속하는 key: value 줄만 메타. 빈 줄(또는 다른 텍스트)부터 본문.
- scope 생략 시: repo: 있으면 repo, 없으면 playbook(범용). 레포 특정이면 scope: repo 또는 repo: 를 꼭.
- knowledge.md 는 append-only. 승격 추적은 마스터 ledger 가 하니 표식 불필요.
-->
