"""Derived coordination scenarios; none is a reconstructed 2026 incident."""
from .data import geometry

FAMILIES = ('local_smoke', 'growing_fire', 'false_alarm', 'competing_incidents', 'aircraft_unavailable', 'wind_shift')


def get_scenario(identifier='regional-000', pack=None):
    from .data import load_pack
    pack = load_pack() if pack is None else pack
    identifiers = [f'regional-{index:03d}' for index in range(len(FAMILIES))]
    if identifier not in identifiers:
        raise ValueError('Unknown regional scenario')
    index = identifiers.index(identifier)
    grid = geometry(pack)
    distance = 4 if index in (0, 2) else 2
    west = [int(grid['width']*.4)-distance, grid['height']//2]
    east = [int(grid['width']*.7)-distance, grid['height']//2]
    events = [] if index == 2 else [dict(tick=0, kind='ignition', location=west)]
    if index == 3:
        events.append(dict(tick=12, kind='ignition', location=east))
    if index == 5:
        events.append(dict(tick=20, kind='wind', wind=[-1, 1]))
    reports = [dict(id='report-0', observed_at=0, received_at=0, location=west,
                    text='Smoke reported; location and cause require verification.', source='synthetic-resident')]
    if index == 3:
        reports.append(dict(id='report-1', observed_at=12, received_at=14, location=east,
                            text='A separate smoke column is reported.', source='synthetic-responder'))
    if index in (1, 5):
        events.append(dict(tick=6, kind='ignition', location=[west[0], west[1]+2]))
        reports.append(dict(id='report-1', observed_at=6, received_at=8, location=[west[0],west[1]+2],
                            text='Additional smoke beside the first report.', source='synthetic-lookout'))
    return dict(id=identifier, family=FAMILIES[index], seed=20000+index, events=events, reports=reports,
                wind=[1, 0] if index != 1 else [2, 0], aircraft_ready_at=20 if index == 4 else 0,
                minimum_ticks=20, synthetic=True)
