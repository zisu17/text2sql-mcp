# Text2SQL 메타데이터 그래프 노드·관계 설계 v2

작성 기준: 2026-11-21. 이 문서는 `semantic-definitions/definitions/olist/`의 Ossie 0.3 정의를 기준으로 한 설계안이다. 그래프 적재 코드 변경이나 Neo4j 재적재 결과를 뜻하지 않는다.

> 이 설계안은 현재 Aura 게시 스키마와 다르다. 현재 MCP 조회 계약은 [graphdb-mcp-design.md](graphdb-mcp-design.md)를 따른다.

## 1. 목적과 범위

그래프는 승인된 시맨틱 정의와 실제 DB 카탈로그의 **조회용 투영**이다. 정의의 정본은 `sales.ossie.yaml`, `sales.ontology.yaml`, `sales.mapping.yaml`이다. 지표 계산과 승인 판정은 정의 및 Text2SQL 엔진이 수행한다. 그래프는 정의 탐색, 물리 컬럼 계보, 변경 영향 조회를 맡는다.

현재 Olist 정의에는 데이터셋 10개, 필드 61개, 지표 22개, 시맨틱 연결 9개가 있다. `order_lines`와 `review_lines`는 여러 물리 테이블을 읽는 SQL 데이터셋이다. 따라서 종전의 `Dataset → Table` 최대 1개와 단순 `Field → Column` 매핑을 확장한다.

핵심 범위는 **고정된 정의의 계보**다. 사용자의 질문에 따라 실행 시점에 추가하는 차원, 필터, 조인은 실행 계획 또는 질의 로그의 계보로 따로 기록한다. 정적 그래프가 그 경로까지 안다고 표시하지 않는다.

## 2. 노드와 식별자

업무 노드는 `Model`, `Dataset`, `Field`, `Metric`, `SourceQuery`, `JoinRule`, `Table`, `Column`의 8종이다. `SourceQuery`는 SQL 데이터셋의 소스 SELECT를 버전별로 보관한다. `JoinRule`은 Ossie `relationships`의 규칙 하나를 승인 상태와 키 검증 결과까지 포함해 버전별로 보관한다. 앞의 여섯 종류에는 `Definition` 라벨을 함께 붙인다. 운영 노드 `ActiveVersion`, `LoadRun`, `CatalogRun`, `CatalogLock`, `SchemaChange`도 유지한다.

`SourceQuery`의 ID는 `query:order_lines`처럼 SQL 데이터셋별로 만들고 `Dataset -[:HAS_SOURCE_QUERY]-> SourceQuery`로 연결한다. `JoinRule`의 ID는 `join:order_items_to_orders`처럼 규칙 이름으로 만들고 `Model -[:HAS_JOIN_RULE]-> JoinRule`로 연결한다. 같은 데이터셋 쌍에 여러 조인 규칙을 저장할 수 있다.

| 대상 | 식별 키 | 수명 |
|---|---|---|
| Definition | `(tenant, model, version_id, id)` | 정의 버전별 불변 스냅샷 |
| Table | `(tenant, source_id, catalog, schema, name)` | 최신 카탈로그 관측값, 삭제 시 `removed_at` |
| Column | `(tenant, source_id, catalog, schema, table, name)` | 최신 카탈로그 관측값, 삭제 시 `removed_at` |
| ActiveVersion | `(tenant, model)` | 현재 활성 `version_id`를 가리키는 포인터 |

`version_id`는 세 정의 파일의 SHA-256 내용 해시다. 파일을 상대 경로 순서로 정렬하고 각 파일의 상대 경로·바이트 길이·원본 바이트를 해시 입력에 넣어 파일 경계도 구분한다. Git 커밋은 `git_commit`으로 별도 보관한다. 로컬에 아직 커밋이 없는 상태에서도 같은 정의 묶음을 같은 버전으로 식별할 수 있다. 기존 `ActiveVersion.commit`을 읽는 코드는 전환 기간에만 지원하고 새 적재에서는 `version_id`를 사용한다. `revision`은 사람이 읽는 변경 번호이며 버전 키가 아니다.

## 3. 관계 계약

화살표는 저장 방향이다. 관계의 시작과 끝은 같은 테넌트여야 한다. 양쪽이 Definition이면 `model`과 `version_id`도 같아야 한다. 적재기는 관계를 만들기 전에 이를 검증한다.

| 관계 | 시작 → 끝 | 의미와 카디널리티 | 주요 속성 |
|---|---|---|---|
| `HAS` | Model → Dataset, Dataset → Field, Dataset → Metric, Model → Metric, Table → Column | 소속. Definition 자식은 부모 정확히 1개 | 없음 |
| `USES_FIELD` | Metric → Field | 지표 식이 참조하는 필드. 0..N개. 한 필드를 여러 역할로 쓰면 관계 1개에 역할 목록 저장 | `roles: [measure, condition, ...]` |
| `HAS_SOURCE_QUERY` | Dataset → SourceQuery | SQL 데이터셋의 소스 SELECT. SQL 데이터셋당 정확히 1개 | 없음 |
| `READS_FROM` | Dataset 또는 SourceQuery → Table | 직접 테이블 소스는 1개, SQL 소스는 1..N개 | `aliases: [...]` |
| `VALUE_DERIVED_FROM` | Field → Column | 필드 값의 물리 입력. 계산식은 0..N개. 상수는 0개 | `lineage_method: direct/sql_ast`, `type_at_approval`(선택) |
| `ROW_DEPENDS_ON` | SourceQuery → Column | SQL 소스의 행 포함·행 수·그룹 형성에 영향을 주는 물리 컬럼. 0..N개 | `roles: [join, filter, group, having, ...]`, `type_at_approval`(선택) |
| `HAS_JOIN_RULE` | Model → JoinRule | 모델에 선언된 조인 규칙. 규칙당 부모 Model 정확히 1개 | 없음 |
| `FROM_DATASET`, `TO_DATASET` | JoinRule → Dataset | 규칙의 출발·도착 데이터셋. 각각 정확히 1개 | 없음 |
| `REFERENCES` | Table → Table | DB가 실제 선언한 외래키. 시맨틱 JoinRule을 대신하지 않음 | `name`, `columns`, `ref_columns` |

`HAS`는 기존 관계다. 종전의 세 종류 `DERIVED_FROM`은 위의 `USES_FIELD`, `READS_FROM`, `VALUE_DERIVED_FROM`으로 의미를 분리한다. `ROW_DEPENDS_ON`을 추가해 SQL의 조인·필터 때문에 생기는 행 집합 의존성을 표현한다. `READS_FROM`만으로는 어떤 테이블의 모든 컬럼이 지표 값에 영향을 준다고 추론하지 않는다.

`VALUE_DERIVED_FROM`은 **값의 출처**, `ROW_DEPENDS_ON`은 **결과 행 집합의 후보 영향**이다. 후자는 보수적인 영향 분석이다. 예컨대 유일성이 검증된 `LEFT JOIN`에서 오른쪽 테이블의 필드를 전혀 사용하지 않는 지표는 실제 영향이 없을 수도 있다. 조회 결과에는 영향 종류를 반드시 함께 반환한다.

`JoinRule.declared_n_to_1`은 정의에 적힌 대상 키와 맞는다는 판정이다. 실제 데이터의 중복 검사 결과는 `data_verified_at` 및 적재 진단에 별도로 남긴다. 승인 상태와 데이터 검증 상태를 혼동하지 않는다. 같은 데이터셋 쌍에 이름이 다른 조인 규칙을 허용하며 `(tenant, model, version_id, name)`의 중복은 적재기가 거부한다.

## 4. SQL 데이터셋 계보 생성

1. Ossie 정의와 카탈로그를 읽어 각 데이터셋의 `source`를 `table` 또는 `query`로 분류한다. 임의 SQL 정규식으로 컬럼을 추출하지 않는다. Text2SQL 엔진이 받아들이는 SQL 방언의 파서를 사용한다.
2. 직접 테이블 소스는 `Dataset -[:READS_FROM]-> Table` 하나를 만든다. 각 필드 식의 컬럼 참조를 같은 테이블의 `Column`에 `VALUE_DERIVED_FROM`으로 연결한다.
3. SQL 소스는 `Dataset -[:HAS_SOURCE_QUERY]-> SourceQuery`를 만들고 `FROM`과 `JOIN`의 별칭을 물리 테이블로 해석해 SourceQuery의 `READS_FROM`을 만든다. 출력 표현식별 원본 컬럼을 계산해 해당 `Field`에서 물리 `Column`으로 `VALUE_DERIVED_FROM`을 만든다. 단순 별칭뿐 아니라 계산식의 모든 입력 컬럼을 포함한다.
4. `ON`, `WHERE`, `GROUP BY`, `HAVING` 등의 컬럼은 SourceQuery의 `ROW_DEPENDS_ON`으로 연결한다. `ORDER BY`는 `LIMIT` 또는 윈도 계산처럼 결과의 값·행 선택을 바꿀 때만 포함한다. 같은 컬럼이 여러 역할을 가지면 관계 하나에 역할 목록을 저장한다.
5. 지표 식의 필드 참조를 `USES_FIELD`로 만든다. 집계 대상과 `FILTER (WHERE ...)` 조건의 역할을 분리한다. 표현식에서 파싱하거나 해석하지 못한 참조가 있으면 조용히 생략하지 않고 진단에 남긴다.

현재 정의에서 기대하는 대표 경로는 다음과 같다.

```text
Metric(revenue)
  ├─USES_FIELD {measure}──▶ Field(order_lines.price)
  │                            └─VALUE_DERIVED_FROM──▶ Column(order_items.price)
  └─USES_FIELD {condition}─▶ Field(order_lines.order_status)
                               └─VALUE_DERIVED_FROM──▶ Column(orders.order_status)

Dataset(order_lines) ─HAS_SOURCE_QUERY──▶ SourceQuery(order_lines)
  ├─READS_FROM──▶ Table(order_items), Table(orders), Table(customers), Table(sellers)
  └─ROW_DEPENDS_ON {join}──▶ Column(order_items.order_id), Column(orders.order_id), ...

Model(olist_sales) ─HAS_JOIN_RULE──▶ JoinRule(order_items_to_orders)
  ├─FROM_DATASET──▶ Dataset(order_items)
  └─TO_DATASET────▶ Dataset(orders)
```

`order_lines`의 `JOIN`에는 `orders.customer_id`·`customers.customer_id`와 `order_items.seller_id`·`sellers.seller_id`도 있다. `review_lines`도 `order_reviews.order_id`와 `orders.order_id`를 사용한다. JoinRule 9개는 Ossie에 선언된 조인 규칙으로 별도 유지한다. SQL 소스의 고정 조인을 이 9개와 자동으로 같은 관계라고 간주하지 않는다.

## 5. 버전·승인·카탈로그 규칙

- 세 정의 파일을 한 묶음으로 검증하고 버전 해시를 계산한다. 새 버전의 Definition 노드와 관계를 먼저 적재한다. 연결 누락 및 끝점·카디널리티 위반을 확인한 뒤 `ActiveVersion.version_id`를 원자적으로 교체한다.
- 이전 Definition 버전은 즉시 삭제하지 않는다. 활성 버전과 비교·복구에 필요한 기간을 정해 보존한다. 카탈로그 노드는 공유하므로 이전 정의 버전의 관계를 삭제할 때에도 Table·Column을 삭제하지 않는다.
- `Metric.status=approved`와 `Metric.usable=true`는 계보 완전성을 뜻하지 않는다. 적재 진단에서 `lineage_status=complete/partial/unresolved`를 별도로 산출한다. 승인된 지표의 물리 계보를 사용하는 기능은 `complete`만 확정 결과로 표시한다.
- 카탈로그의 `Column.raw_type`은 최신 관측값이다. 승인 시점의 타입은 버전별 `VALUE_DERIVED_FROM`/`ROW_DEPENDS_ON` 관계에 기록한다. 기준 타입이 없으면 타입 변경 결과는 `UNKNOWN`이다.
- `SchemaChange`와 실행 기록에는 고유한 `run_id`를 부여한다. 재시도 적재가 같은 이벤트를 중복 생성하지 않도록 적재 단계에서 검사한다.

Neo4j의 복합 유일 제약은 키 속성의 존재를 보장하지 않는다. 필수 속성과 관계 끝점·부모 수는 적재기에서 검증한다. Enterprise의 키 제약은 선택할 수 있지만 Community에서도 동작하는 적재 계약을 기본으로 둔다.

v2 노드 키의 최소 제약은 다음과 같다. 기존 `version` 키로 만든 제약은 마이그레이션 동안 별도로 유지한다.

```cypher
CREATE CONSTRAINT definition_key_v2 IF NOT EXISTS
FOR (n:Definition)
REQUIRE (n.tenant, n.model, n.version_id, n.id) IS UNIQUE;

CREATE CONSTRAINT active_key IF NOT EXISTS
FOR (n:ActiveVersion)
REQUIRE (n.tenant, n.model) IS UNIQUE;
```

관계는 먼저 양 끝 노드를 식별한 뒤 `MERGE`한다. JoinRule은 종류 접두어가 있는 ID로 `MERGE`하고 출발·도착 Dataset을 각각 연결한다. 다른 관계의 역할 목록과 승인 시점 타입은 `MERGE` 후 `SET`으로 갱신한다. 적재 전에는 동일한 `name`의 중복 규칙을 검사한다.

```cypher
MATCH (from:Dataset {tenant:$tenant, model:$model,
                     version_id:$version_id, ref:$from_ref})
MATCH (to:Dataset {tenant:$tenant, model:$model,
                   version_id:$version_id, ref:$to_ref})
MATCH (m:Model {tenant:$tenant, model:$model,
                version_id:$version_id})
MERGE (j:Definition:JoinRule {tenant:$tenant, model:$model,
                              version_id:$version_id, id:$join_id})
SET j.name = $name,
    j.from_columns = $from_columns,
    j.to_columns = $to_columns,
    j.status = $status,
    j.declared_n_to_1 = $declared_n_to_1
MERGE (m)-[:HAS_JOIN_RULE]->(j)
MERGE (j)-[:FROM_DATASET]->(from)
MERGE (j)-[:TO_DATASET]->(to);
```

## 6. 조회 계약

모든 Definition 조회는 `tenant`, `model`, `ActiveVersion.version_id`를 고정한다. 물리 컬럼에서 지표를 찾을 때는 두 결과를 합친다.

1. `Column ←VALUE_DERIVED_FROM─ Field ←USES_FIELD─ Metric`: 직접 값·조건 의존성. `USES_FIELD.roles`를 반환한다.
2. `Column ←ROW_DEPENDS_ON─ SourceQuery ←HAS_SOURCE_QUERY─ Dataset ─HAS→ Metric`: SQL 소스의 행 집합 후보 의존성. `ROW_DEPENDS_ON.roles`를 반환한다.

같은 지표가 두 경로에 걸리면 두 영향 종류를 보존한다. 승인 여부를 가리지 않는 변경 영향 조회와 `status=approved`·`usable=true`를 적용하는 실행 후보 조회는 API를 분리한다. 질문 시점에 추가된 조인·필터가 포함된 **실제 실행 계보**는 이 정적 결과에 합쳐 보여줄 수 있지만 출처를 구별해야 한다.

예를 들어 다음 두 조회를 합쳐 영향 목록을 만든다. 매개변수에는 Column의 전체 물리 키를 사용한다. 두 번째 결과의 `row_set_candidate`는 확정된 값 출처가 아니다.

```cypher
MATCH (a:ActiveVersion {tenant:$tenant, model:$model})
MATCH (c:Column {tenant:$tenant, source_id:$source_id,
                 catalog:$catalog, schema:$schema,
                 table:$table, name:$column})
MATCH (m:Metric {tenant:$tenant, model:$model,
                 version_id:a.version_id})
      -[u:USES_FIELD]->(f:Field)-[:VALUE_DERIVED_FROM]->(c)
RETURN DISTINCT m.ref AS metric, 'value' AS impact, u.roles AS roles;
```

```cypher
MATCH (a:ActiveVersion {tenant:$tenant, model:$model})
MATCH (c:Column {tenant:$tenant, source_id:$source_id,
                 catalog:$catalog, schema:$schema,
                 table:$table, name:$column})
MATCH (d:Dataset {tenant:$tenant, model:$model,
                  version_id:a.version_id})
      -[:HAS_SOURCE_QUERY]->(q:SourceQuery)-[r:ROW_DEPENDS_ON]->(c)
MATCH (d)-[:HAS]->(m:Metric)
RETURN DISTINCT m.ref AS metric, 'row_set_candidate' AS impact,
                r.roles AS roles;
```

## 7. 적재 검증 기준

- 현재 정의의 10개 Dataset, 61개 Field, 22개 Metric, SourceQuery 2개, JoinRule 9개를 손실 없이 식별한다. SQL 소스 두 개를 직접 테이블 소스로 잘못 분류하지 않는다.
- `revenue`의 값 계보에 `order_items.price`와 `orders.order_status`가 나타난다. `avg_review_score`의 값 계보에는 `order_reviews.review_score`와 `orders.order_status`가 나타난다.
- `orders.order_id` 변경 영향 조회에는 `order_lines`와 `review_lines`의 조인 키 의존성이 표시된다. 이를 물리 값 출처로 잘못 표시하지 않는다.
- 모든 관계의 양 끝점과 테넌트가 일치한다. 두 Definition 사이의 관계는 버전도 일치한다. Definition 자식의 부모 수는 정확히 1개다.
- 같은 버전을 다시 적재해도 노드·관계 수가 늘지 않는다. 해석할 수 없는 SQL·카탈로그 누락은 `partial` 또는 `unresolved`로 드러난다.
- 이전 버전과 새 버전을 각각 조회할 수 있고 활성 포인터를 옮겨도 이전 버전의 계보가 바뀌지 않는다.

## 8. 도입 순서

1. 적재기에서 v2 노드와 관계를 새 `version_id` 아래에 병행 생성한다. 기존 관계를 즉시 덮어쓰지 않는다.
2. SQL 소스 두 개와 지표 6개의 계보를 먼저 대조한다. Olist 전체 정의에서 역방향 영향 조회를 검증한다.
3. 활성 포인터를 v2 버전으로 전환한다. 구형 `DERIVED_FROM`을 읽는 조회를 새 관계 기반으로 옮긴다.
4. 구형 관계와 버전은 보존 기간 및 복구 절차를 확인한 뒤 정리한다.

온톨로지·매핑 파일의 `Concept`를 그래프에서 직접 탐색할 요구가 생기면 별도 버전 노드와 매핑 관계를 추가한다. 지금은 세 파일을 함께 버전 식별하되 업무 개념의 개별 인스턴스를 이 메타데이터 그래프에 적재하지 않는다.

## 참고

- 블로그: `zisu17.github.io/_posts/2026-11-06-[Text2SQL]-메타데이터와-지표-계보를-위한-GraphDB-설계.md`
- 정의: `semantic-definitions/definitions/olist/sales.ossie.yaml`, `sales.ontology.yaml`, `sales.mapping.yaml`
- Neo4j Cypher Manual: https://neo4j.com/docs/cypher-manual/current/schema/constraints/create-constraints/
- Neo4j Cypher Manual: https://neo4j.com/docs/cypher-manual/current/clauses/merge/
