import requests

class VolumioClient:
    def __init__(self, host):
        self.host = host
        self.session = requests.Session()

    def get_state(self):
        url = f"http://{self.host}/api/v1/getState"
        try:
            r = self.session.get(url, timeout=3)
            if r.status_code == 200:
                return r.json()
            return {}
        except Exception:
            # Network or connection error: return None to signal unreachable host
            return None

    def send_command(self, cmd, **params):
        url = f"http://{self.host}/api/v1/commands/?cmd={cmd}"
        if params:
            url += "&" + "&".join(f"{k}={v}" for k, v in params.items())
        try:
            self.session.get(url, timeout=3)
        except Exception:
            pass

    def send_seek(self, position_seconds):
        secs = int(position_seconds)
        msecs = int(position_seconds * 1000)
        candidates = [
            {'value': secs},
            {'position': secs},
            {'seek': msecs},
            {'seek': secs},
        ]
        for params in candidates:
            try:
                self.send_command('seek', **params)
                break
            except Exception:
                continue

    def _parse_time(self, v):
        if v is None:
            return None
        try:
            return float(v)
        except Exception:
            pass
        try:
            parts = [float(x) for x in str(v).split(':')]
            if len(parts) == 3:
                return parts[0] * 3600 + parts[1] * 60 + parts[2]
            if len(parts) == 2:
                return parts[0] * 60 + parts[1]
            return float(parts[0])
        except Exception:
            return None

    def _deep_find_all(self, obj, candidates):
        res = []
        if obj is None:
            return res
        if isinstance(obj, dict):
            for k, v in obj.items():
                kl = k.lower()
                for cand in candidates:
                    if cand in kl:
                        res.append(v)
                res.extend(self._deep_find_all(v, candidates))
        elif isinstance(obj, list):
            for item in obj:
                res.extend(self._deep_find_all(item, candidates))
        return res

    def parse_status_times(self, status):
        if not isinstance(status, dict):
            return None, None

        # collect candidate values (prefer top-level if present)
        seek_candidates = []
        dur_candidates = []
        for k in ('seek', 'position', 'elapsed', 'progress'):
            if k in status:
                seek_candidates.append(status.get(k))
        for k in ('duration', 'trackDuration', 'totalTime', 'length', 'tracklength'):
            if k in status:
                dur_candidates.append(status.get(k))
        # extend with deep search
        seek_candidates.extend(self._deep_find_all(status, ['seek', 'position', 'elapsed', 'progress']))
        dur_candidates.extend(self._deep_find_all(status, ['duration', 'trackduration', 'totaltime', 'length', 'tracklength', 'time', 'total']))

        def _to_number(v):
            t = self._parse_time(v)
            if t is None:
                return None
            return float(t)

        # try combinations of raw vs milliseconds conversion for seek/duration
        pairs = []
        for sv_raw in seek_candidates:
            svn = _to_number(sv_raw)
            if svn is None:
                continue
            for dv_raw in dur_candidates:
                dvn = _to_number(dv_raw)
                if dvn is None:
                    continue
                for sv_factor in (1.0, 1.0/1000.0):
                    for dv_factor in (1.0, 1.0/1000.0):
                        try:
                            svs = svn * sv_factor
                            dvs = dvn * dv_factor
                        except Exception:
                            continue
                        # plausible: seek between 0 and duration (allow small overshoot) and duration reasonable (<10h)
                        if 0 <= svs <= dvs * 1.1 and 0 < dvs < 36000:
                            pairs.append((svs, dvs, sv_factor, dv_factor))
        # prefer pairs where duration was not treated as milliseconds (dv_factor == 1.0), then smaller duration
        if pairs:
            pairs.sort(key=lambda x: (0 if x[3] == 1.0 else 1, x[1]))
            seek_s, dur_s, _, _ = pairs[0]
        else:
            # fallback: take first sensible duration (prefer not-converted)
            dur_s = None
            for dv_raw in dur_candidates:
                dvn = _to_number(dv_raw)
                if dvn is None:
                    continue
                if 0 < dvn < 36000:
                    dur_s = dvn
                    break
                if dvn > 1000:
                    # try ms->s
                    try:
                        cand = dvn / 1000.0
                        if 0 < cand < 36000:
                            dur_s = cand
                            break
                    except Exception:
                        pass
            seek_s = None
            for sv_raw in seek_candidates:
                svn = _to_number(sv_raw)
                if svn is None:
                    continue
                if 0 <= svn <= (dur_s or float('inf')) * 1.1:
                    seek_s = svn
                    break
            # final fallback convert if needed
            if seek_s is None:
                for sv_raw in seek_candidates:
                    svn = _to_number(sv_raw)
                    if svn is None:
                        continue
                    if svn > 1000:
                        seek_s = svn / 1000.0
                        break
                    seek_s = svn
            if dur_s is None:
                dur_s = 0

        return seek_s, dur_s