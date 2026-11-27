from backtesting.performance_metrics import calculate_metrics
def strategy_report(trades):
    return calculate_metrics(trades)
