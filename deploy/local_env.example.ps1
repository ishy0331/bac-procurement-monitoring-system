# deploy/local_env.example.ps1 - Template for deploy/local_env.ps1
#
# Copy to deploy\local_env.ps1 and fill in the Neo4j password:
#     copy deploy\local_env.example.ps1 deploy\local_env.ps1
#
# deploy\start_server.ps1 loads deploy\local_env.ps1 automatically.
# deploy\local_env.ps1 is in .gitignore: never commit it (it holds a password).

$env:NEO4J_URI      = "neo4j://127.0.0.1:7687"
$env:NEO4J_USERNAME = "neo4j"
$env:NEO4J_PASSWORD = "CHANGE-ME"
$env:NEO4J_DATABASE = "neo4j"
