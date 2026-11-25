# GraphDB 조회용 MCP 계약

작성 기준: 2026-11-24(MCP 0.3.0). 게시 스키마: semantic-definitions 075a232 이후의 roles 추가분까지.

## 책임과 그래프 범위

semantic-definitions가 Ossie 정의와 DB 카탈로그를 Neo4j에 게시한다. 이 저장소의 MCP는 그래프를 조회만 한다. SQL 생성·실행, 원본 DB 행 조회, 정의 수정과 적재는 MCP에 없다.

| 범위 | 현재 구조 |
|---|---|
| 정의 노드 | Model, Dataset, Field, Metric. 공통 Definition 라벨과 tenant/model/version/id 키 |
| 물리 노드 | Table, Column. tenant/source_id/catalog/schema/객체명 복합 키 |
| 운영 노드 | ActiveVersion(tenant, model, commit), LoadRun, CatalogRun |
| 관계 | HAS, DERIVED_FROM, JOINS, REFERENCES |

Concept, SourceQuery, JoinRule과 POINTS_TO, USES_FIELD, VALUE_DERIVED_FROM, ROW_DEPENDS_ON 등은 현재 게시 그래프에 없다. [v2 설계 문서](semantic-lineage-graph-v2.md)는 구현 이력이다.

## 활성 버전

각 도구는 하나의 읽기 트랜잭션에서 ActiveVersion.commit과 Model.version의 일치를 확인한다. 모든 정의 탐색은 같은 tenant, model, version으로 제한한다.

```cypher
MATCH (a:ActiveVersion {tenant:$tenant, model:$model})
OPTIONAL MATCH (root:Model {tenant:$tenant, model:$model})
WHERE root.version = a.commit
RETURN a.commit AS version, count(root) AS roots
```

응답의 version_id는 이 commit을 담는 기존 MCP 호환 필드다. 활성 노드가 없으면 NO_ACTIVE_VERSION, 해당 Model이 정확히 하나가 아니면 SCHEMA_INCOMPATIBLE로 처리한다.

## 일곱 조회 도구와 스냅샷 경로

| 도구 | 입력 | 반환 |
|---|---|---|
| graph_list_models | 없음 | 활성 모델과 version_id |
| graph_search_definitions | query, 선택 kinds/model/limit | Dataset·Field·Metric 검색 |
| graph_get_dataset | dataset, 선택 model | 필드·지표·소스·물리 테이블 |
| graph_get_metric_lineage | dataset, metric, 선택 model | 역할별 필드·값 계보·조인 영향 후보 |
| graph_get_column_impact | 물리 컬럼 전체 키, 선택 model | 컬럼에서 활성 지표로 이어지는 영향 |
| graph_list_join_rules | 선택 dataset/model | JOINS 관계의 이름·키·상태·n_to_1 |
| graph_find_join_path | datasets(2~6개), 선택 model/max_hops(1~6, 기본 4) | 이웃한 두 데이터셋마다 승인된 최단 경로. 없으면 승인 전·N:1 아닌 연결로 가는 최단 경로 |

Concept가 게시되지 않으므로 graph_get_concept_mapping은 제거했다. 검색 kinds의 Concept도 허용하지 않는다.

성공한 MCP 도구 호출의 데이터는 `structuredContent` 한 곳에 담는다. 클라이언트는 이 필드에서 결과를 읽는다. 오류 메시지는 `content`로 반환한다.

실행용 시맨틱 모델 전체(스냅샷)는 0.3.0부터 MCP 도구가 아니다. 예전 graph_get_model_snapshot 도구는 LLM이 모델 전체를 맥락에 넣을 수 있어 지웠다. MCP 리소스로도 내지 않는다. Claude Code는 리소스를 읽는 도구를 LLM에 자동으로 주기 때문이다. 대신 HTTP 앱이 `/mcp`와 같은 Bearer 인증 뒤에 `GET /snapshot`, `GET /snapshot/{model}`을 둔다. 이 경로는 t2s만 부른다. 반환은 tenant, model, version_id, definition(Ossie 실행용 정의)이고 응답 상한은 1 MiB다. 오류는 `{"error": {"code", "message"}}`와 HTTP 상태(NOT_FOUND·NO_ACTIVE_VERSION 404, DB_UNAVAILABLE 503, 그 밖 500)다. stdio 실행에는 이 경로가 없다.

graph_find_join_path는 먼저 t2s 엔진이 승인된 연결로 보는 관계만 따라간다. 모든 관계가 status='approved'이고 n_to_1=true여야 한다. 그런 경로가 없는 쌍은 unreachable에 넣고, 선언된 JOINS 전부(승인 전, N:1 아님 포함)로 구한 최단 경로를 unapproved_paths로 함께 준다. hop마다 status와 n_to_1이 있다. 이 경로로 조인하는 것은 막지 않으며 답에 밝히게 한다. 방향은 보지 않고 allShortestPaths로 같은 길이의 경로를 모두 구해 이름 순으로 최대 5개를 돌려준다. 둘 이상이면 ambiguous, 더 있으면 has_more를 붙인다. hop마다 관계의 from·to와 키를 주고, 경로를 따라가는 방향이 관계의 to→from이면 reverse=true다. 이 방향은 한 행 쪽에서 여러 행 쪽으로 가므로 행이 늘어난다. 없는 데이터셋은 NOT_FOUND, 같은 이름이 이웃하면 INVALID_INPUT이다. 가변 길이 상한은 정수 검증 뒤 Cypher에 넣고 나머지 값은 매개변수로 넘긴다. 안내용이다. t2s는 승인 안 된 조인을 막지 않고 결과의 unapproved_join 경고로 알린다.

직접 테이블 소스에는 Dataset→Table DERIVED_FROM이 있다. SQL 소스에는 이 관계가 없고 Dataset.source에 조회문 텍스트가 있다. 소스가 [catalog.][schema.]table 식별자이면 테이블, 아니면 조회문으로 본다. t2s 엔진의 SOURCE_NAME, semantic-definitions의 plan.py와 같은 규칙이다. 소스가 비어 있으면 SCHEMA_INCOMPATIBLE이다. SQL 소스의 테이블 목록은 Dataset→Field→Column의 DERIVED_FROM에서 구한다. 이 목록은 필드 계보에 드러난 테이블만 포함하므로 모든 소스 테이블이라는 보장은 없다. tables_basis=field_lineage로 표시한다. 소스 SQL은 실행하지 않는다.

지표 계보는 Metric-[DERIVED_FROM {roles, role}]->Field-[:DERIVED_FROM]->Column을 따른다. roles는 한 필드가 그 지표에서 맡는 역할 전부이고 role은 대표 값 하나다. roles가 없는 예전 그래프는 [role]로 읽는다. roles에 measure·condition이 있으면 value_columns에 넣고 join이 있으면 row_set_candidates에 넣는다. 둘 다 있으면 양쪽에 넣는다. row_set_candidates는 값 변경 확정이 아닌 조인 영향 후보다. 물리 컬럼 역방향 조회도 impact(value 또는 row_set_candidate)와 row_set_candidate 여부를 함께 준다. 컬럼은 source_id/catalog/schema/table/column 전체 키로 식별한다.

조인은 Dataset-[JOINS]->Dataset에서 읽는다. status와 n_to_1을 그대로 보여준다. n_to_1은 선언된 키와 일치한다는 뜻이며 실제 데이터의 유일성 검증이 아니다. 이전 data_verified 속성은 없다.

검색은 semantic-definitions가 게시할 때 만드는 전문 검색 인덱스 `definition_text`(Definition 노드의 ref·name·description·synonyms, Lucene `cjk` 분석기)를 Cypher 25 `SEARCH` 절로 읽는다. 정렬은 참조명이 정확히 같은 항목, 이름이 정확히 같은 항목, 동의어 하나가 정확히 같은 항목, 그다음 Lucene 관련도 점수(BM25) 순이고 항목마다 `score`를 준다. 입력은 낱말마다 Lucene 특수 문자를 이스케이프해 OR로 묶으므로 연산자로 해석되지 않는다. 인덱스 하나가 모든 tenant·model·version을 담고 `SEARCH`의 LIMIT이 tenant·model 필터보다 먼저 걸리므로, 후보 수를 색인된 Definition 노드 수와 같게 받아 이 모델의 항목이 잘리지 않게 한 뒤 거른다. 인덱스가 없거나 아직 준비 중이면(GQLSTATUS 22N69 등) 이름·참조명·설명·동의어의 부분 일치로 대신 찾고 `search: substring`으로 표시한다. 검색은 기본 1건을 반환한다. 추가 후보가 있으면 `has_more`로 알린다. 첫 후보가 부족할 때는 `limit`을 높여 다시 검색할 수 있다. 상한은 50건이다. 도구 응답 제한은 128 KiB, 스냅샷은 1 MiB, 읽기 트랜잭션 시간 상한은 5초다.

Neo4j 오류는 서버 오류 코드로 나눈다. Neo.ClientError.Statement.*·Neo.ClientError.Schema.*는 조회가 게시 그래프 모양과 맞지 않는 것이라 SCHEMA_INCOMPATIBLE이다. 인증·권한(Neo.ClientError.Security.*), 트랜잭션 시간 초과(Neo.ClientError.Transaction.*), 일시 오류, 드라이버·연결 오류는 DB_UNAVAILABLE이다. 로그에는 오류 코드와 예외 이름만 남기고 메시지와 쿼리 값은 남기지 않는다. Cypher는 코드에 고정하고 입력은 매개변수로 전달한다. execute_read()는 DB 권한 경계가 아니므로 DB 차원의 쓰기 차단이 필요하면 읽기 전용 계정을 사용한다.

## 확인

```sh
UV_CACHE_DIR=/tmp/text2sql-mcp-uv-cache uv run --no-sync python -m unittest discover -s tests -v
```

로컬 통합 검사는 Neo4j 접속 환경변수와 GRAPH_INTEGRATION_TEST=1을 설정한다. 활성 커밋, order_lines.revenue의 값·조인 계보, orders.order_id 영향, 9개 JOINS가 대표 확인 항목이다.

## 참고

- [semantic-definitions 게시 계약](https://github.com/zisu17/semantic-definitions/blob/main/docs/graphdb.md)
- [Neo4j Python Driver 읽기 트랜잭션](https://neo4j.com/docs/python-manual/current/transactions/)
