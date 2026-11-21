# GraphDB 조회용 MCP 계약

작성 기준: 2026-11-21. 게시 스키마: semantic-definitions 3f48bcd.

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

## 여섯 조회 도구

| 도구 | 입력 | 반환 |
|---|---|---|
| graph_list_models | 없음 | 활성 모델과 version_id |
| graph_search_definitions | query, 선택 kinds/model/limit | Dataset·Field·Metric 검색 |
| graph_get_dataset | dataset, 선택 model | 필드·지표·소스·물리 테이블 |
| graph_get_model_snapshot | 선택 model | 활성 버전의 실행용 시맨틱 모델 |
| graph_get_metric_lineage | dataset, metric, 선택 model | 역할별 필드·값 계보·조인 영향 후보 |
| graph_get_column_impact | 물리 컬럼 전체 키, 선택 model | 컬럼에서 활성 지표로 이어지는 영향 |
| graph_list_join_rules | 선택 dataset/model | JOINS 관계의 이름·키·상태·n_to_1 |

Concept가 게시되지 않으므로 graph_get_concept_mapping은 제거했다. 검색 kinds의 Concept도 허용하지 않는다.

직접 테이블 소스에는 Dataset→Table DERIVED_FROM이 있다. SQL 소스에는 이 관계가 없고 Dataset.source에 SELECT 텍스트가 있다. SQL 소스의 테이블 목록은 Dataset→Field→Column의 DERIVED_FROM에서 구한다. 이 목록은 필드 계보에 드러난 테이블만 포함하므로 모든 소스 테이블이라는 보장은 없다. tables_basis=field_lineage로 표시한다. 소스 SQL은 실행하지 않는다.

지표 계보는 Metric-[DERIVED_FROM {role}]->Field-[:DERIVED_FROM]->Column을 따른다. role=measure/condition이면 value_columns에 넣고 role=join이면 row_set_candidates에 넣는다. 후자는 값 변경 확정이 아닌 조인 영향 후보다. 물리 컬럼 역방향 조회도 value와 row_set_candidate를 구분한다. 컬럼은 source_id/catalog/schema/table/column 전체 키로 식별한다.

조인은 Dataset-[JOINS]->Dataset에서 읽는다. status와 n_to_1을 그대로 보여준다. n_to_1은 선언된 키와 일치한다는 뜻이며 실제 데이터의 유일성 검증이 아니다. 이전 data_verified 속성은 없다.

검색은 이름·참조명·설명·동의어의 부분 일치다. limit 기본 20, 상한 50이다. 응답 제한은 128 KiB, 읽기 트랜잭션 시간 상한은 5초다. Cypher는 코드에 고정하고 입력은 매개변수로 전달한다. execute_read()는 DB 권한 경계가 아니므로 DB 차원의 쓰기 차단이 필요하면 읽기 전용 계정을 사용한다.

## 확인

```sh
UV_CACHE_DIR=/tmp/text2sql-mcp-uv-cache uv run --no-sync python -m unittest discover -s tests -v
```

로컬 통합 검사는 Neo4j 접속 환경변수와 GRAPH_INTEGRATION_TEST=1을 설정한다. 활성 커밋, order_lines.revenue의 값·조인 계보, orders.order_id 영향, 9개 JOINS가 대표 확인 항목이다.

## 참고

- [semantic-definitions 게시 계약](https://github.com/zisu17/semantic-definitions/blob/main/docs/graphdb.md)
- [Neo4j Python Driver 읽기 트랜잭션](https://neo4j.com/docs/python-manual/current/transactions/)
