function renderDiff(oldText, newText) {
    if (oldText === newText) return '<em>(no changes)</em>';

    const oldLines = (oldText || '').split('\n');
    const newLines = (newText || '').split('\n');

    const lcs = computeLCS(oldLines, newLines);
    let html = '<div class="diff-view">';

    let oi = 0, ni = 0, li = 0;
    while (oi < oldLines.length || ni < newLines.length) {
        if (li < lcs.length && oi < oldLines.length && ni < newLines.length && oldLines[oi] === lcs[li] && newLines[ni] === lcs[li]) {
            html += `<div class="diff-context"> ${escapeHtml(oldLines[oi])}</div>`;
            oi++; ni++; li++;
        } else {
            if (oi < oldLines.length && (li >= lcs.length || oldLines[oi] !== lcs[li])) {
                html += `<div class="diff-removed">-${escapeHtml(oldLines[oi])}</div>`;
                oi++;
            }
            if (ni < newLines.length && (li >= lcs.length || newLines[ni] !== lcs[li])) {
                html += `<div class="diff-added">+${escapeHtml(newLines[ni])}</div>`;
                ni++;
            }
            if (oi < oldLines.length && ni < newLines.length && oldLines[oi] === newLines[ni]) {
                html += `<div class="diff-context"> ${escapeHtml(oldLines[oi])}</div>`;
                oi++; ni++;
            }
        }
    }

    html += '</div>';
    return html;
}

function computeLCS(a, b) {
    const m = a.length, n = b.length;
    const dp = Array.from({length: m + 1}, () => new Array(n + 1).fill(0));
    for (let i = 1; i <= m; i++) {
        for (let j = 1; j <= n; j++) {
            if (a[i - 1] === b[j - 1]) {
                dp[i][j] = dp[i - 1][j - 1] + 1;
            } else {
                dp[i][j] = Math.max(dp[i - 1][j], dp[i][j - 1]);
            }
        }
    }
    // Backtrack
    const result = [];
    let i = m, j = n;
    while (i > 0 && j > 0) {
        if (a[i - 1] === b[j - 1]) {
            result.unshift(a[i - 1]);
            i--; j--;
        } else if (dp[i - 1][j] > dp[i][j - 1]) {
            i--;
        } else {
            j--;
        }
    }
    return result;
}
