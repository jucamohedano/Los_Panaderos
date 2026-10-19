"""Public-observation policies for CPU-only regional acceptance runs."""
import math


def passive(observation):
    return {}


class CooperativeBaseline:
    def __call__(self, observation):
        action = dict(requests=[], authorizations=[], releases=[], messages=[])
        action['authorizations'] = [offer['id'] for offer in observation['offers'] if offer['status'] == 'offered']
        sectors = {sector['id']: sector for sector in observation['sectors']}
        reports = [sector for sector in observation['sectors'] if sector['id'].startswith('report:')]
        fire = [cell for cell in observation['sensor_memory'] if cell['burning'] and cell['observed_at'] == observation['minute']]
        for unit in observation['resources']:
            if unit['state'] == 'on_scene':
                target = sectors[unit['sector_id']]['center']
                if not any(math.dist(target, [cell['x'], cell['y']]) <= 3 for cell in fire):
                    action['releases'].append(unit['id'])
            elif unit['state'] == 'available' and reports:
                recent_reports = [report for report in observation['reports']
                                  if observation['minute']-report['observed_at'] <= 12]
                active = [sector for sector in reports if any(math.dist(sector['center'], [cell['x'],cell['y']]) <= 3 for cell in fire)]
                candidates = active or [sectors['report:'+report['id']] for report in recent_reports]
                if candidates:
                    sector = candidates[0 if unit['home_base'] == 'valverde' else -1]
                    action['requests'].append(dict(resource_id=unit['id'], sector_id=sector['id'], mission='suppress'))
        notices = {(ack['source'], ack.get('purpose')) for ack in observation['acknowledgements']
                   if ack['kind'] == 'message_delivered'}
        for district in observation['districts']:
            if district['status'] != 'unwarned' or (district['id'], 'evacuate') in notices:
                continue
            if any(math.dist(report['location'], district['home']) <= 6 for report in observation['reports']):
                action['messages'].append(dict(recipient=district['id'], purpose='evacuate'))
        return action
