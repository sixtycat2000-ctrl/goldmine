# goldmine fzf widget (v2 item 19 / F3, 03 §5.6) — ^G searches the index.
# Install: source this file from ~/.zshrc, e.g.
#   source /path/to/goldmine/scripts/shell/goldmine.fzf.zsh
# Enter opens the pick via `goldmine open` (audio seeks, code jumps to line);
# nothing selected = nothing happens. Graceful when fzf is absent.

goldmine-fzf-widget() {
  emulate -L zsh
  if ! command -v fzf >/dev/null 2>&1; then
    print -u2 "goldmine: fzf not found — fix: brew install fzf"
    return 1
  fi
  local sel
  sel=$(goldmine search "$BUFFER" --format fzf --limit 20 2>/dev/null \
    | fzf --delimiter '\t' --with-nth=1,4 --ansi \
        --preview 'goldmine open --path {1} --kind {2} --dry-run 2>&1' \
        --preview-window right:60%:wrap \
        --header 'enter=open · esc=cancel') || return 0
  local -a fields
  fields=("${(ps.\t.)sel}")
  goldmine open --path "${fields[1]}" --kind "${fields[2]}" || return 0
  zle reset-prompt
}
zle -N goldmine-fzf-widget
bindkey '^G' goldmine-fzf-widget
