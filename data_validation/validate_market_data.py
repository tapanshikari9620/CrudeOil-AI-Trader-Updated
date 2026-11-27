import pandas as pd
def validate_market_frame(df: pd.DataFrame) -> dict:
    required={"timestamp","open","high","low","close","volume"}
    missing=sorted(required-set(df.columns))
    invalid=0
    if not missing:
        invalid=int(((df.high < df[["open","close","low"]].max(axis=1)) |
                     (df.low > df[["open","close","high"]].min(axis=1)) |
                     (df[["open","high","low","close"]] <= 0).any(axis=1) | (df.volume < 0)).sum())
    return {"rows":len(df),"missing_columns":missing,"invalid_rows":invalid,"passed":not missing and invalid==0}
