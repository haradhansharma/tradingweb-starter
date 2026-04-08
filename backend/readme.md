# How to Add a New Indicator (3 steps)

### 1. Write the calculation method

__Open `indicator_engine.py` and define your calculation logic using Pandas:__

```python
def _calc_vwap(self, df, **params):
    # your pandas calculation
    # e.g., vwap = (df['volume'] * (df['high'] + df['low']) / 2).cumsum() / df['volume'].cumsum()
    return float(value)
```
### Add registry entry in `INDICATOR_REGISTRY`:
```python
"vwap": {
    "timeframes": ["15m", "1h"],
    "params": {},
    "order": 9,
    "label": "VWAP",
    "format": "price",
    "decimals": 0,
    "font_size": "11px",
    "default_class": "text-gray-300 dark:text-gray-400",
},
```