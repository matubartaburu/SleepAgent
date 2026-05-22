#!/usr/bin/env bash
# install-hooks.sh — instala los git hooks del proyecto.
# Corré esto una vez después de clonar: `bash scripts/install-hooks.sh`

set -e
cd "$(dirname "$0")/.."

mkdir -p .git/hooks

cat > .git/hooks/pre-commit <<'EOF'
#!/usr/bin/env bash
# pre-commit — bloquea commits que contengan secrets o archivos sensibles.
# Si tenés que saltarlo a propósito: `git commit --no-verify` (NO LO HAGAS para .env).

set -e

# Lista de patrones que NUNCA deben estar en un commit.
BLOCKED_FILES_REGEX='^(\.env$|\.env\.[^.]+$|.*\.pem$|.*\.key$|data/.*\.xml$|data/.*\.zip$)'

# Patrones de secrets en el contenido (heurística — no perfecta pero atrapa lo común).
BLOCKED_CONTENT_REGEX='\b(sk-ant-[a-zA-Z0-9_-]{20,}|sk-proj-[a-zA-Z0-9_-]{20,}|secret_[a-zA-Z0-9]{20,}|ntn_[a-zA-Z0-9]{20,}|AC[a-f0-9]{32}\b)'

violation=0

# 1) Bloquear paths sensibles
while IFS= read -r file; do
    if [[ -n "$file" && "$file" =~ $BLOCKED_FILES_REGEX ]]; then
        echo "❌ BLOQUEADO: '$file' no puede commitearse (regla de seguridad)."
        echo "   Si es .env: nunca commitéalo, está en .gitignore por algo."
        echo "   Si es un secreto que ya no usás, rotalo (rotate) y bórralo localmente."
        violation=1
    fi
done < <(git diff --cached --name-only --diff-filter=ACM)

# 2) Bloquear contenido sospechoso de secrets en archivos staged
while IFS= read -r file; do
    if [[ -z "$file" ]]; then continue; fi
    # Solo archivos de texto
    if [[ "$file" =~ \.(py|md|yml|yaml|toml|json|sh|env\.example|cfg|ini)$ ]]; then
        if git diff --cached -- "$file" | grep -E "$BLOCKED_CONTENT_REGEX" > /dev/null 2>&1; then
            echo "❌ BLOQUEADO: posible secreto en '$file'."
            echo "   Encontré un patrón que parece API key / token."
            echo "   Si es falso positivo, podés saltearlo con --no-verify (pensalo dos veces)."
            violation=1
        fi
    fi
done < <(git diff --cached --name-only --diff-filter=ACM)

if [[ $violation -ne 0 ]]; then
    echo ""
    echo "Commit abortado. Resolvé los problemas y reintentá."
    exit 1
fi

exit 0
EOF

chmod +x .git/hooks/pre-commit
echo "✓ pre-commit hook instalado en .git/hooks/pre-commit"
echo "  Bloquea: .env, *.pem, data/*.xml + heurística de secrets en contenido."
