import math
from datetime import timedelta


def forecast(stock, daily_units, as_of, lead_days=14, safety_days=7, source='history'):
    """Transparent baseline, not a promise. Does not include unconfirmed supply.

    Zero and missing data are distinct. A shortage suppresses observed demand,
    so the UI labels the estimate and its source rather than claiming certainty.
    """
    if stock is None or daily_units is None or not math.isfinite(daily_units) or daily_units < 0:
        return {'days_left':None,'stockout_date':None,'reorder_date':None,'recommended_units':None,
                'level':'unknown','source':source,'daily_units':daily_units}
    if stock <= 0:
        days = 0.0
    elif daily_units == 0:
        return {'days_left':None,'stockout_date':None,'reorder_date':None,'recommended_units':0,
                'level':'no_demand','source':source,'daily_units':0}
    else:
        days = stock/daily_units
    date = as_of+timedelta(days=math.ceil(days))
    return {'days_left':round(days,1),'stockout_date':date.isoformat(),
            'reorder_date':(date-timedelta(days=lead_days+safety_days)).isoformat(),
            'recommended_units':max(0,math.ceil(daily_units*(lead_days+safety_days)-stock)),
            'level':'critical' if days<=lead_days else 'warning' if days<=lead_days+safety_days else 'ok',
            'source':source,'daily_units':round(daily_units,3)}
