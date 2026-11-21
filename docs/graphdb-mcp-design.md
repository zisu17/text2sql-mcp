# GraphDB 조회용 MCP 설계

작성 기준: 2026-11-21  
상태: 구현된 조회 계약

## 1. 책임과 경계

이 저장소의 MCP 서버는 Neo4j에 게시된 메타데이터를 **조회만** 한다. 정의 파일 검증, 원본 DB 카탈로그 수집, 그래프 적재와 활성 버전 전환은 `semantic-definitions`의 책임이다. MCP 서버는 `semantic-definitions`를 런타임 패키지로 가져오지 않는다. 두 저장소는 아래의 그래프 읽기 계약으로 연결한다.

```mermaid
flowchart LR
  A[semantic-definitions<br/>정의·카탈로그 검증과 게시] --> B[(Neo4j)]
  B --> C[text2sql-mcp<br/>읽기 전용 MCP 서버]
  C --> D[로컬 MCP 클라이언트]
```

MCP가 반환하는 내용은 정의 탐색과 정적 계보다. 지표 계산, SQL 생성·검증·실행, 사용자의 질의 시점에 추가된 필터·조인의 실행 계보는 이 서버의 범위에 넣지 않는다. 그래프의 승인 상태를 SQL 실행 허가로 해석하지 않는다.

현재 저장소의 `graphdb/load_original.py`는 최초 설계 재현용이다. MCP 패키지는 이 파일을 가져오거나 실행하지 않는다. `graphdb/compose.yml`은 로컬 Neo4j 실행에 계속 사용할 수 있다.

## 2. 실제 적재 스키마와 호환성

조회 계약은 `semantic-definitions/graphdb/plan.py`와 `graphdb/neo4j_writer.py`의 현재 게시 결과를 따른다.

| 범주 | 현재 구조 | 조회 규칙 |
|---|---|---|
| 정의 노드 | `Model`, `Dataset`, `Field`, `Metric`, `SourceQuery`, `JoinRule`, `Concept`에 `Definition`·`GraphEntity` 라벨 | `tenant`, `model`, `version_id`로 활성 스냅샷 한정 |
| 물리 노드 | `Table`, `Column`에 `CatalogEntity`·`GraphEntity` 라벨 | `tenant`, `source_id`, `catalog`, `schema`, 객체 이름으로 식별 |
| 활성 버전 | `ActiveVersion(tenant, model, version_id)-[:POINTS_TO]->Model` | 속성과 포인터 대상 Model의 버전이 같아야 함 |
| 주요 관계 | `HAS`, `HAS_SOURCE_QUERY`, `READS_FROM`, `USES_FIELD`, `VALUE_DERIVED_FROM`, `ROW_DEPENDS_ON`, `HAS_JOIN_RULE`, `FROM_DATASET`, `TO_DATASET`, `MAPS_TO` | 의미별로 분리해 반환 |

기존 `graphdb/load_original.py`의 `version`, `commit`, `DERIVED_FROM`, `JOINS` 관계는 MCP의 읽기 계약에 포함하지 않는다. 활성 `POINTS_TO`가 없는 구형 그래프는 `SCHEMA_INCOMPATIBLE` 오류로 알린다. [이 저장소의 v2 설계 문서](semantic-lineage-graph-v2.md)는 설계 이력이며 실제 게시 코드와 차이가 있다. 예를 들어 현재 게시 코드는 `Concept`와 `MAPS_TO`를 만든다.

모든 정의 조회는 다음 시작 조건을 사용한다. 같은 도구 호출에서 여러 쿼리가 필요하면 **하나의 읽기 트랜잭션**에서 실행해 활성 버전이 중간에 바뀌지 않도록 한다. 정의 노드를 추가로 탐색할 때도 `version_id` 일치를 확인한다.

```cypher
MATCH (a:ActiveVersion {tenant: $tenant, model: $model})
      -[:POINTS_TO]->(root:Model {tenant: $tenant, model: $model})
WHERE root.version_id = a.version_id
RETURN root, a.version_id AS version_id
```

`Table`·`Column`은 정의 버전과 무관한 공유 노드다. 물리 객체를 직접 검색해서 과거 정의의 연결을 섞지 않고 위의 활성 루트에서 시작하거나 물리 노드에서 활성 정의 노드로 역방향 탐색한다. `ActiveVersion`이나 포인터가 없거나 서로 다른 버전을 가리키면 빈 검색 결과로 처리하지 않는다.

## 3. MCP 도구

도구 이름에는 `graph_` 접두어를 붙여 향후 Text2SQL 실행 도구와 구별한다. 서버 설정의 `GRAPH_TENANT`가 조회 범위를 고정한다. `model` 입력은 선택 사항이고 생략하면 `GRAPH_DEFAULT_MODEL`을 쓴다. 모델을 명시해도 같은 테넌트의 활성 모델만 조회한다.

| 도구 | 입력 | 반환 |
|---|---|---|
| `graph_list_models` | 없음 | 설정된 테넌트의 활성 모델 목록과 `version_id` |
| `graph_search_definitions` | `query`, 선택 `kinds`, `model`, `limit` | 활성 `Dataset`·`Field`·`Metric`·`Concept`의 이름, 설명, 동의어, 상태와 식별자 |
| `graph_get_dataset` | `dataset`, 선택 `model` | 필드·지표, 소스 종류, 읽는 물리 테이블, SQL 소스 여부와 소스 SQL |
| `graph_get_metric_lineage` | `dataset`, `metric`, 선택 `model` | 지표 식·상태, 참조 필드와 역할, 값 출처 컬럼, 행 집합 영향 후보 컬럼 |
| `graph_get_column_impact` | `source_id`, `catalog`, `schema`, `table`, `column`, 선택 `model` | 영향을 받을 수 있는 지표와 `value` 또는 `row_set_candidate` 영향 종류 |
| `graph_list_join_rules` | 선택 `dataset`, `model` | 조인 규칙의 출발·도착 데이터셋, 키, 선언 상태, `declared_n_to_1`, `data_verified` |
| `graph_get_concept_mapping` | `concept`, 선택 `model` | 개념에 연결된 필드와 그 필드의 물리 값 출처 |

### 입력과 결과의 세부 계약

- 지표는 `metric` 이름만으로 전역 식별하지 않는다. `dataset + metric`을 함께 받아 `Metric.id=metric:<dataset>.<metric>`에 대응시킨다.
- 필드는 `dataset.field`와 `Field.id`를 함께 반환한다. 컬럼은 `source_id/catalog/schema/table/column` 전체 키를 반환한다. 같은 이름의 객체를 합치지 않는다.
- `graph_search_definitions`는 이름·`ref`·설명·동의어의 부분 일치를 검색한다. 초기 Olist 규모에서는 활성 정의 노드만 검색하고 별도 검색 인덱스는 만들지 않는다. `limit` 기본값은 20, 상한은 50이다. 정렬을 고정하고 한 건을 더 읽어 `has_more`를 계산한다.
- `graph_get_dataset`의 `source_kind`는 `table` 또는 `sql_query`다. 직접 테이블 소스는 `Dataset-[:READS_FROM]->Table`, SQL 소스는 `Dataset-[:HAS_SOURCE_QUERY]->SourceQuery-[:READS_FROM]->Table`을 따른다. SQL 소스가 여러 테이블을 읽는 것을 허용한다. 저장된 소스 SQL은 **텍스트로만 반환**하고 실행하지 않는다.
- `graph_get_metric_lineage`에서 `USES_FIELD.roles`를 `measure`·`condition` 등으로 보존한다. `VALUE_DERIVED_FROM`은 `value` 출처다. SQL 소스의 `ROW_DEPENDS_ON`은 `row_set_candidate`로 별도 배열에 둔다. 후자를 지표 값의 원본 컬럼이라고 표시하지 않는다.
- `graph_get_column_impact`는 값 경로 `Column←VALUE_DERIVED_FROM─Field←USES_FIELD─Metric`과 행 집합 경로 `Column←ROW_DEPENDS_ON─SourceQuery←HAS_SOURCE_QUERY─Dataset─HAS→Metric`을 각각 조회한다. 한 지표가 양쪽에 나오면 영향 종류를 둘 다 반환한다.
- `graph_list_join_rules`는 `status`와 `data_verified`를 그대로 보여준다. `declared_n_to_1=true`는 정의상의 키 일치이며 실제 데이터의 유일성 검증을 뜻하지 않는다. 초안 규칙을 숨기지 않고 상태를 명시한다.
- `graph_get_concept_mapping`은 `Concept-[:MAPS_TO]->Field`만 명시된 매핑 경로로 취급한다. 이 관계에 승인 속성이 없으므로 승인 여부를 추정하지 않는다. 개념 간 관계나 지표 매핑을 그래프에 없는 상태에서 추론하지 않는다.

성공 결과는 목록도 최상위 JSON 객체로 감싼다. 공통 필드는 `tenant`, `model`, `version_id`다. 검색은 일치 항목이 없어도 빈 목록을 반환한다. 단건 조회의 미존재, 활성 버전 누락, 포인터 불일치, Neo4j 연결 실패는 각각 `NOT_FOUND`, `NO_ACTIVE_VERSION`, `SCHEMA_INCOMPATIBLE`, `DB_UNAVAILABLE`로 구별한다. 결과에는 SQL 질의 문자열이나 접속 정보를 넣지 않는다.

`graph_get_metric_lineage(dataset="order_lines", metric="revenue")`의 기대 형태는 다음과 같다. 실제 `version_id`와 전체 컬럼 키는 호출 시점의 그래프에서 가져온다.

```json
{
  "tenant": "olist",
  "model": "olist_sales",
  "version_id": "<active-version-id>",
  "metric": {"dataset": "order_lines", "ref": "revenue", "status": "approved"},
  "fields": [
    {"ref": "order_lines.price", "roles": ["measure"], "value_columns": ["order_items.price"]},
    {"ref": "order_lines.order_status", "roles": ["condition"], "value_columns": ["orders.order_status"]}
  ],
  "row_set_candidates": [
    {"column": "orders.order_id", "roles": ["join"]}
  ]
}
```

위 예시의 짧은 컬럼 이름은 가독성을 위한 표기다. 실제 응답은 모든 컬럼에 전체 물리 키를 포함한다. 현재 게시 코드에는 `lineage_status`나 `Metric.usable` 속성이 없다. 경로가 존재한다는 이유만으로 계보가 완전하거나 SQL 실행이 가능하다고 선언하지 않는다. 매핑이 없는 필드는 빈 `value_columns`로 드러낸다.

## 4. 구현 배치와 실행

```text
text2sql-mcp/
├── pyproject.toml
├── src/text2sql_mcp/
│   ├── __init__.py
│   ├── config.py              # 환경변수와 입력 기본값
│   ├── contracts.py           # 도구별 입력·출력 형식
│   ├── graph_repository.py    # 고정 Cypher, 읽기 트랜잭션
│   └── server.py              # MCP 도구 등록 및 stdio 진입점
├── tests/
│   ├── test_graph_tools.py
│   └── test_graph_integration.py
├── docs/graphdb-mcp-design.md
└── graphdb/                 # 기존 로컬 인프라·최초 설계 재현 코드
```

Python MCP SDK 2.x의 `MCPServer`로 도구를 등록하고 첫 전송 방식은 로컬 `stdio`로 한다. 서버 시작 전에 표준 출력에 로그를 쓰지 않는다. 오류·운영 로그는 표준 오류로 보낸다. 원격 클라이언트가 필요한 시점에만 Streamable HTTP를 검토한다.

| 환경변수 | 용도 |
|---|---|
| `NEO4J_URI` | Bolt 접속 주소 |
| `NEO4J_USER` | Neo4j 사용자 |
| `NEO4J_PASSWORD` | Neo4j 암호 |
| `NEO4J_DATABASE` | 데이터베이스명, 기본 `neo4j` |
| `GRAPH_TENANT` | MCP 인스턴스가 조회할 테넌트, 로컬 기본 `olist` |
| `GRAPH_DEFAULT_MODEL` | `model` 입력을 생략할 때 사용할 모델, 로컬 기본 `olist_sales` |

`DATABASE_URL`과 정의 파일 경로는 MCP 서버에 필요하지 않다. 설정값과 암호는 Git에 저장하지 않는다. 드라이버는 프로세스 동안 공유하고 세션은 도구 호출마다 연다. 각 조회는 데이터베이스명을 지정한 `execute_read()`에서 실행한다. 사용자 입력은 Cypher 매개변수로만 전달한다. 도구가 실행할 쿼리는 코드에 고정하고 임의 Cypher 입력, 동적 관계 종류, 쓰기 쿼리, 소스 SQL 실행 기능을 두지 않는다. 트랜잭션 시간 상한은 5초, 목록 결과는 최대 50건, 도구 응답은 최대 128 KiB로 제한한다. 계보·영향 결과가 응답 상한을 넘으면 조용히 잘라내지 않고 오류로 알린다.

현재 `graphdb/compose.yml`은 Neo4j Community 이미지를 사용한다. Community에서는 역할 기반 읽기 전용 계정을 만들 수 없으므로 MCP의 **기능상 읽기 전용**은 고정 쿼리와 `execute_read()` 경계로 구현한다. `execute_read()`는 접근 제어 수단이 아니므로 DB 자체의 쓰기 권한까지 막아야 하는 환경에서는 RBAC를 지원하는 Neo4j 배포와 `reader` 권한 계정을 사용한다. 로컬 서버는 현재처럼 `127.0.0.1`에만 노출한다.

## 5. 구현 범위와 검증

1. 패키지 진입점과 Neo4j 연결 설정을 만들었다. 읽기 트랜잭션에서 활성 버전·포인터 일치를 확인하고 오류를 구별한다.
2. `graph_list_models`, `graph_search_definitions`, `graph_get_dataset`을 구현했다. 정의 버전과 물리 소스의 연결을 확인한다.
3. 지표 계보, 컬럼 영향, 조인 규칙, 개념 매핑 도구를 구현했다. 값 출처와 행 집합 후보를 분리한다.
4. MCP 클라이언트에서 `tools/list`와 실제 호출을 확인했다. 로컬 Neo4j 통합 테스트와 Neo4j가 필요 없는 프로토콜 테스트를 제공한다.

완료 기준은 다음과 같다.

- 활성 `version_id`가 응답에 나오며 과거 버전의 정의 노드가 결과에 섞이지 않는다.
- `order_lines`가 SQL 소스로 표시되고 `order_items`, `orders` 등 여러 물리 테이블이 조회된다.
- `revenue`의 값 출처에 `order_items.price`, 조건 출처에 `orders.order_status`가 나타난다.
- `orders.order_id`의 영향 결과에는 SQL 조인 키에 따른 `row_set_candidate`가 나타나며 이를 `value`로 표기하지 않는다.
- `JoinRule`의 `status`, `declared_n_to_1`, `data_verified`가 서로 다른 의미로 전달된다.
- 도구 호출 전후 Neo4j 노드·관계·활성 포인터가 변하지 않는다. 임의 Cypher나 쓰기 도구가 `tools/list`에 없다.

## 참고

- [MCP Python SDK: 서버 실행과 stdio](https://github.com/modelcontextprotocol/python-sdk/blob/main/docs/run/index.md)
- [MCP Python SDK: v2 마이그레이션](https://github.com/modelcontextprotocol/python-sdk/blob/main/docs/migration.md)
- [Neo4j Python Driver: 읽기 트랜잭션](https://neo4j.com/docs/python-manual/current/transactions/)
- [Neo4j: Community 사용자 권한 제한](https://neo4j.com/docs/operations-manual/current/authentication-authorization/manage-users/)
