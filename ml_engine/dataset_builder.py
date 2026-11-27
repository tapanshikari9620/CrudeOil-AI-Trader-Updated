def chronological_split(frame, train_fraction=.70, validation_fraction=.15):
    if not 0 < train_fraction < 1 or not 0 < validation_fraction < 1 or train_fraction+validation_fraction >= 1:
        raise ValueError("Invalid chronological split fractions")
    n=len(frame); a=int(n*train_fraction); b=int(n*(train_fraction+validation_fraction))
    return frame.iloc[:a].copy(),frame.iloc[a:b].copy(),frame.iloc[b:].copy()
