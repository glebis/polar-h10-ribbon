"""Central HRV metrics computation — single source of truth.

All metrics computed from cleaned NN intervals per Task Force (1996) standards.
Every dashboard/API should call these functions instead of reimplementing.

References:
- Task Force of ESC/NASPE. Circulation. 1996;93(5):1043-1065
- Shaffer F & Ginsberg JP. Front Public Health. 2017;5:258
- Brennan M et al. IEEE Trans Biomed Eng. 2001;48(11):1342-1347
- Richman JS & Moorman JR. Am J Physiol. 2000;278(6):H2039-H2049
- Peng CK et al. Chaos. 1995;5(1):82-87
"""
import math


def clean_rr(rr_list: list[int | float], method: str = "malik") -> list[float]:
    """Artifact correction: remove ectopic beats and interpolate.

    Filters:
    1. Physiological range: 300-2000ms (30-200 bpm)
    2. Malik rule: reject if RR differs >20% from local median (5-beat window)
    3. Interpolate gaps with local median

    Returns cleaned NN intervals.
    """
    if len(rr_list) < 3:
        return list(rr_list)

    # Step 1: physiological range
    filtered = [rr for rr in rr_list if 300 <= rr <= 2000]
    if len(filtered) < 3:
        return filtered

    # Step 2: Malik criterion — reject if >20% from local median
    nn = []
    window = 5
    for i, rr in enumerate(filtered):
        start = max(0, i - window // 2)
        end = min(len(filtered), i + window // 2 + 1)
        local = sorted(filtered[start:end])
        median = local[len(local) // 2]
        if abs(rr - median) / median <= 0.20:
            nn.append(rr)
        else:
            nn.append(median)  # interpolate with local median

    return nn


def rmssd(nn: list[float]) -> float | None:
    """Root mean square of successive differences.
    Requires ≥4 NN intervals. Standard short-term vagal metric.
    """
    if len(nn) < 4:
        return None
    diffs_sq = [(nn[i+1] - nn[i])**2 for i in range(len(nn)-1)]
    return math.sqrt(sum(diffs_sq) / len(diffs_sq))


def sdnn(nn: list[float]) -> float | None:
    """Standard deviation of NN intervals (sample SD, n-1).
    Requires ≥10 NN intervals.
    Note: clinically meaningful only for defined recording durations
    (5-min short-term or 24-hour). Do not compare across durations.
    """
    if len(nn) < 10:
        return None
    mean = sum(nn) / len(nn)
    variance = sum((x - mean)**2 for x in nn) / (len(nn) - 1)  # sample variance
    return math.sqrt(variance)


def pnn50(nn: list[float]) -> float | None:
    """Percentage of successive NN differences > 50ms.
    Requires ≥10 NN intervals. Parasympathetic marker.
    """
    if len(nn) < 10:
        return None
    diffs = [abs(nn[i+1] - nn[i]) for i in range(len(nn)-1)]
    count = sum(1 for d in diffs if d > 50)
    return count / len(diffs) * 100


def pnn20(nn: list[float]) -> float | None:
    """Percentage of successive NN differences > 20ms."""
    if len(nn) < 10:
        return None
    diffs = [abs(nn[i+1] - nn[i]) for i in range(len(nn)-1)]
    count = sum(1 for d in diffs if d > 20)
    return count / len(diffs) * 100


def poincare(nn: list[float]) -> dict | None:
    """Poincaré plot metrics: SD1, SD2, SD1/SD2 ratio.
    Computed directly from NN intervals (not from averaged RMSSD/SDNN).

    SD1 = SD of points perpendicular to identity line (short-term variability)
    SD2 = SD of points along identity line (long-term variability)
    """
    if len(nn) < 10:
        return None
    x = nn[:-1]
    y = nn[1:]
    n = len(x)

    # Difference and sum series
    diff = [(y[i] - x[i]) for i in range(n)]
    summ = [(y[i] + x[i]) for i in range(n)]

    mean_diff = sum(diff) / n
    mean_summ = sum(summ) / n

    sd1 = math.sqrt(sum((d - mean_diff)**2 for d in diff) / (n - 1)) / math.sqrt(2)
    sd2 = math.sqrt(sum((s - mean_summ)**2 for s in summ) / (n - 1)) / math.sqrt(2)

    ratio = sd1 / sd2 if sd2 > 0 else 0
    return {"sd1": round(sd1, 1), "sd2": round(sd2, 1), "ratio": round(ratio, 3)}


def sample_entropy(nn: list[float], m: int = 2, r_factor: float = 0.2) -> float | None:
    """Sample entropy (SampEn). Measures signal complexity/unpredictability.

    m: embedding dimension (standard: 2)
    r_factor: tolerance as fraction of SD (standard: 0.2)
    Requires ≥200 NN intervals.

    Lower = more regular = less healthy.
    """
    if len(nn) < 200:
        return None
    # cap at 500 beats for performance (O(n²) algorithm)
    nn = nn[-500:]

    mean_nn = sum(nn) / len(nn)
    sd_nn = math.sqrt(sum((x - mean_nn)**2 for x in nn) / len(nn))
    if sd_nn == 0:
        return None
    r = r_factor * sd_nn

    def count_templates(data, template_len):
        n = len(data) - template_len  # correct: N - m
        count = 0
        for i in range(n):
            for j in range(i + 1, n):
                match = True
                for k in range(template_len):
                    if abs(data[i + k] - data[j + k]) > r:
                        match = False
                        break
                if match:
                    count += 1
        return count

    b = count_templates(nn, m)
    a = count_templates(nn, m + 1)

    if b == 0 or a == 0:
        return None
    return -math.log(a / b)


def dfa_alpha1(nn: list[float]) -> float | None:
    """Detrended Fluctuation Analysis, short-term exponent (α1).

    Scales 4-16 beats (standard short-term window).
    Requires ≥64 NN intervals.

    Healthy: 0.75-1.0. >1.0 = loss of fractal complexity.
    """
    n = len(nn)
    if n < 64:
        return None

    mean_nn = sum(nn) / n
    integrated = []
    cumsum = 0
    for v in nn:
        cumsum += (v - mean_nn)
        integrated.append(cumsum)

    scales = [s for s in [4, 5, 6, 7, 8, 10, 12, 16] if s <= n // 4]
    if len(scales) < 4:
        return None

    log_n, log_f = [], []
    for s in scales:
        num_segments = n // s
        fluctuations = []
        for seg in range(num_segments):
            start = seg * s
            segment = integrated[start:start + s]
            xs = list(range(s))
            mx = (s - 1) / 2
            my = sum(segment) / s
            num = sum((x - mx) * (y - my) for x, y in zip(xs, segment))
            den = sum((x - mx) ** 2 for x in xs)
            slope = num / den if den > 0 else 0
            intercept = my - slope * mx
            resid = [(segment[i] - (slope * i + intercept)) ** 2 for i in range(s)]
            fluctuations.append(math.sqrt(sum(resid) / s))
        if fluctuations:
            mean_f = sum(fluctuations) / len(fluctuations)
            if mean_f > 0:
                log_n.append(math.log(s))
                log_f.append(math.log(mean_f))

    if len(log_n) < 3:
        return None
    nl = len(log_n)
    mx = sum(log_n) / nl
    my = sum(log_f) / nl
    num = sum((x - mx) * (y - my) for x, y in zip(log_n, log_f))
    den = sum((x - mx) ** 2 for x in log_n)
    return round(num / den, 3) if den > 0 else None


def triangular_index(nn: list[float], bin_width: float = 7.8125) -> float | None:
    """HRV Triangular Index: total NN / max histogram bin.
    bin_width: 1/128s = 7.8125ms (Task Force standard).
    Requires ≥100 NN intervals. Robust to artifacts.
    """
    if len(nn) < 100:
        return None
    bins = {}
    for v in nn:
        b = int(v / bin_width)
        bins[b] = bins.get(b, 0) + 1
    peak_count = max(bins.values())
    return round(len(nn) / peak_count, 1)


def rmssd_sdnn_ratio(nn: list[float]) -> float | None:
    """RMSSD/SDNN ratio — vagal-to-total variability index."""
    r = rmssd(nn)
    s = sdnn(nn)
    if r is None or s is None or s == 0:
        return None
    return round(r / s, 3)


def compute_all(nn: list[float]) -> dict:
    """Compute all available metrics from a cleaned NN interval series.
    Returns dict with metric names as keys, None for insufficient data.
    """
    result = {
        "n": len(nn),
        "mean_nn": round(sum(nn) / len(nn), 1) if nn else None,
        "mean_hr": round(60000 / (sum(nn) / len(nn))) if nn else None,
        "rmssd": None, "sdnn": None, "pnn50": None, "pnn20": None,
        "poincare": None, "sample_entropy": None, "dfa_alpha1": None,
        "triangular_index": None, "rmssd_sdnn_ratio": None,
    }
    if not nn:
        return result

    result["rmssd"] = round(rmssd(nn), 1) if rmssd(nn) is not None else None
    result["sdnn"] = round(sdnn(nn), 1) if sdnn(nn) is not None else None
    result["pnn50"] = round(pnn50(nn), 1) if pnn50(nn) is not None else None
    result["pnn20"] = round(pnn20(nn), 1) if pnn20(nn) is not None else None
    result["poincare"] = poincare(nn)
    result["sample_entropy"] = round(sample_entropy(nn), 3) if sample_entropy(nn) is not None else None
    result["dfa_alpha1"] = dfa_alpha1(nn)
    result["triangular_index"] = triangular_index(nn)
    result["rmssd_sdnn_ratio"] = rmssd_sdnn_ratio(nn)
    return result
