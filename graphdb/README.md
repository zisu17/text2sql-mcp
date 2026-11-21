# 최초 GraphDB 설계 재현

2026-11-06 포스팅의 첫 설계를 현재 `semantic-definitions`와 로컬 Olist SQLite에 적용한다. 업무 노드는 `Model`, `Dataset`, `Field`, `Metric`, `Table`, `Column`의 6종이다. 관계는 `HAS`, `DERIVED_FROM`, `JOINS`, `REFERENCES`만 사용한다. `Definition`은 앞의 정의 노드 4종에 공통으로 붙는 라벨이다.

## 실행

```sh
docker compose -f graphdb/compose.yml up -d
uv run --no-project --with-requirements graphdb/requirements.txt \
  python graphdb/load_original.py load \
  --definition /Users/sonjisu/PycharmProjects/semantic-definitions/definitions/olist/sales.ossie.yaml \
  --sqlite /Users/sonjisu/PycharmProjects/text-to-sql-study/data/olist.db
```

Neo4j Browser는 <http://127.0.0.1:7474/browser/>이고 Bolt는 `bolt://127.0.0.1:7687`이다. 두 포트 모두 로컬 주소에만 바인딩한다. 로그인 사용자는 `neo4j`, 암호는 Git에서 제외한 `graphdb/.env`의 `NEO4J_AUTH` 값에서 슬래시 뒤 부분이다. 데이터는 Docker 볼륨 `text2sql-graphdb_graph_data`에 보관한다.

적재 없이 결과를 보려면 `load` 대신 `plan`을 실행한다. 같은 정의를 다시 적재해도 키 제약과 `MERGE` 때문에 노드·관계 수가 늘지 않는다.

## 이번 적재 결과

현재 정의 파일 기준 업무 노드 149개와 운영 노드 3개를 적재했다. `Definition` 라벨 94개는 `Model`·`Dataset`·`Field`·`Metric`과 중복 집계된다. 관계는 `HAS` 140개, `DERIVED_FROM` 94개, `JOINS` 9개다. SQLite에 선언된 외래키가 없어 `REFERENCES`는 0개다.

최초 설계는 SQL 소스 데이터셋을 물리 테이블로 해석하지 않는다. 현재 정의의 `order_lines`와 `review_lines`는 SQL 소스라 `Dataset → Table` 및 해당 `Field → Column` 계보가 없다. 예를 들어 `revenue`에서 참조하는 Field는 2개지만 Column까지 이어지는 경로는 0개다. `orders.order_count`의 필드와 물리 컬럼 경로는 조회된다. 이 공백은 적재 진단에 남겼다.

```cypher
MATCH (n) UNWIND labels(n) AS label
RETURN label, count(*) AS nodes ORDER BY label;

MATCH ()-[r]->()
RETURN type(r) AS relationship, count(*) AS relationships
ORDER BY relationship;

MATCH (m:Metric {ref:'order_count'})-[:DERIVED_FROM]->(f:Field)
      -[:DERIVED_FROM]->(c:Column)
RETURN f.ref, c.table, c.name;
```

이 코드는 첫 설계의 재현용이다. 뒤에 작성한 `SourceQuery`·`JoinRule` 설계의 적재기와 혼동하지 않도록 별도 파일로 둔다.
