param(
    [ValidateSet('lint','test','backtest','parity','db-count')]
    [string]$Task = 'lint',
    [string]$Dataset = '.\tests\fixtures\sample_data',
    [string]$DbA = '.\out_a.db',
    [string]$DbB = '.\out_b.db'
)

switch ($Task) {
    'lint' { ruff check .; break }
    'test' { pytest -q; break }
    'backtest' {
        python -m btcbot.cli stage7-backtest --dataset $Dataset --out $DbA --start 2024-01-01T00:00:00Z --end 2024-01-01T00:10:00Z
        break
    }
    'parity' {
        python -m btcbot.cli stage7-parity --out-a $DbA --out-b $DbB --start 2024-01-01T00:00:00Z --end 2024-01-01T00:10:00Z
        break
    }
    'db-count' { python -m btcbot.cli stage7-db-count --db $DbA; break }
}
