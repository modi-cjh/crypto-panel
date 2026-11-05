"""Engine layer: the generational loop.

Each generation: OBSERVE volatility → BREED (tournament selection, crossover, volatility-scaled mutation, a few random
immigrants) → TEST every child on the training window (fitness) and the unseen window (out-of-sample gate) → SELECT the
next population (elites survive, the rest are replaced) → DEPLOY: the qualified strategy with the best training fitness
becomes the paper-trading champion.
"""
import random

from execution import simulate, fitness
from genome import random_genome, crossover, mutate, SPECIES
import data as D

POPULATION, ELITES, TOURNAMENT, CROSSOVER, IMMIGRANTS = 40, 4, 3, .7, 2
HALL_SIZE, KEEP_GENERATIONS, CURVE_POINTS = 20, 12, 40
GATE = dict(min_ret=0.0, max_dd=25.0, min_trades=8)   # out-of-sample requirements
SWITCH_MARGIN = 1.02                                   # a challenger must beat the champion's fitness by 2 %
# Champion stability: the out-of-sample window moves one bar per hour, so strategies near the gate flip between
# qualified and not. The champion therefore keeps its seat for MIN_TENURE unless it clearly breaks down (RETAIN gate,
# looser than the entry GATE), and a challenger must have qualified in STREAK consecutive runs before it can take over.
MIN_TENURE = 24 * 3600_000
STREAK = 3
RETAIN = dict(min_ret=-2.0, max_dd=30.0)


def downsample(curve, n=CURVE_POINTS):
    if len(curve) <= n:
        return [list(p) for p in curve]
    step = (len(curve) - 1) / (n - 1)
    return [list(curve[round(i * step)]) for i in range(n)]


def evaluate(genes, series, split):
    start, cut, end = split
    tr = simulate(genes, series, start, cut, curve_every=24)
    te = simulate(genes, series, cut, end, curve_every=24)
    m = te['metrics']
    qualified = m['ret'] > GATE['min_ret'] and m['max_dd'] < GATE['max_dd'] and m['trades'] >= GATE['min_trades']
    return dict(fitness=fitness(tr['metrics']), train=tr['metrics'], test=m, qualified=qualified,
                train_curve=downsample(tr['curve']), test_curve=downsample(te['curve']))


def new_node(state, genes, parents, gen, how, series, split):
    state['next_id'] = state.get('next_id', 0) + 1
    node = dict(id=f"s{state['next_id']}", gen=gen, born=gen, died=None, species=genes['species'], parents=parents, how=how, genes=genes)
    node.update(evaluate(genes, series, split))
    state['nodes'][node['id']] = node
    state['stats']['born'] = state['stats'].get('born', 0) + 1
    return node


def init_state(seed, series, split):
    rng = random.Random(seed)
    state = dict(seed=seed, generation=0, nodes={}, population=[], hall=[], champion=None, history=[], stats=dict(born=0, killed=0), next_id=0)
    for i in range(POPULATION):   # start with every species equally represented
        state['population'].append(new_node(state, random_genome(rng, SPECIES[i % len(SPECIES)]), [], 0, 'seed', series, split)['id'])
    update_hall(state)
    return state


def refresh(state, series, split):
    """The data window rolls forward every run: re-score the living population and the hall of fame on it."""
    for nid in set(state['population']) | set(state['hall']) | ({state['champion']} if state['champion'] else set()):
        if nid in state['nodes']:
            state['nodes'][nid].update(evaluate(state['nodes'][nid]['genes'], series, split))
    update_hall(state)


def tournament(pop, rng):
    return max(rng.sample(pop, min(TOURNAMENT, len(pop))), key=lambda n: n['fitness'])


def update_hall(state):
    nodes = state['nodes']
    cand = {nid for nid in set(state['hall']) | set(state['population']) if nid in nodes and nodes[nid]['qualified']}
    state['hall'] = sorted(cand, key=lambda nid: nodes[nid]['fitness'], reverse=True)[:HALL_SIZE]


def generation(state, series, split, obs):
    """Breed, test and select one generation. Returns the new generation's summary row."""
    gen = state['generation'] + 1
    rng = random.Random(f"{state['seed']}-{gen}")
    nodes = state['nodes']
    pop = [nodes[i] for i in state['population']]
    ranked = sorted(pop, key=lambda n: n['fitness'], reverse=True)
    elites = ranked[:ELITES]
    children = []
    for _ in range(IMMIGRANTS):
        children.append(new_node(state, random_genome(rng), [], gen, 'immigrant', series, split))
    while len(children) < POPULATION - ELITES:
        a = tournament(pop, rng)
        if rng.random() < CROSSOVER:
            b = tournament(pop, rng)
            genes, parents, how = crossover(a['genes'], b['genes'], rng), [a['id'], b['id']], 'crossover'
        else:
            genes, parents, how = dict(a['genes']), [a['id']], 'mutation'
        genes = mutate(genes, rng, rate=obs['mutation_rate'])
        children.append(new_node(state, genes, parents, gen, how, series, split))
    new_pop = [n['id'] for n in elites] + [c['id'] for c in children]
    killed = [n['id'] for n in pop if n['id'] not in set(new_pop)]
    for nid in killed:
        nodes[nid]['died'] = gen
    state['stats']['killed'] = state['stats'].get('killed', 0) + len(killed)
    state['population'], state['generation'] = new_pop, gen
    update_hall(state)
    living = [nodes[i] for i in new_pop]
    best = max(living, key=lambda n: n['fitness'])
    row = dict(gen=gen, best=best['fitness'], mean=round(sum(n['fitness'] for n in living) / len(living), 4), best_id=best['id'],
               best_test_ret=best['test']['ret'], qualified=sum(1 for n in living if n['qualified']), born=len(children), killed=len(killed),
               regime=obs['regime'], vol=obs['market'], mutation_rate=obs['mutation_rate'],
               species={sp: sum(1 for n in living if n['species'] == sp) for sp in SPECIES})
    state['history'].append(row)
    return row


def update_streaks(state):
    """Consecutive runs each hall-of-fame strategy has been out-of-sample qualified (call once per run)."""
    old = state.get('streaks', {})
    state['streaks'] = {nid: old.get(nid, 0) + 1 for nid in state['hall']}
    return state['streaks']


def breaks_retain(node):
    t = node['test']
    return t['ret'] < RETAIN['min_ret'] or t['max_dd'] > RETAIN['max_dd']


def choose_champion(state, now_ms):
    """Decide who paper-trades. Returns (champion_id, switched, reason).

    - No champion yet: the best qualified strategy takes over.
    - The champion is replaced early only if it breaks the looser RETAIN gate (clear breakdown on unseen data).
    - Otherwise, after MIN_TENURE, a challenger that has qualified for STREAK consecutive runs and beats the champion's
      training fitness by SWITCH_MARGIN takes over.
    """
    nodes, cur, streaks = state['nodes'], state['champion'], update_streaks(state)
    ready = [nid for nid in state['hall'] if streaks.get(nid, 0) >= STREAK]
    if cur is None or cur not in nodes:
        if not state['hall']:
            return cur, False, None
        state['champion'], state['champion_since'] = state['hall'][0], now_ms
        return state['champion'], True, 'first'
    state.setdefault('champion_since', now_ms)
    champ = nodes[cur]
    if breaks_retain(champ):
        pick = (ready or state['hall'] or [None])[0]
        if pick and pick != cur:
            state['champion'], state['champion_since'] = pick, now_ms
            return pick, True, 'breakdown'
        return cur, False, None
    if now_ms - state['champion_since'] < MIN_TENURE:
        return cur, False, None
    for nid in ready:
        if nid != cur and nodes[nid]['fitness'] > max(champ['fitness'] * SWITCH_MARGIN, champ['fitness'] + .01):
            state['champion'], state['champion_since'] = nid, now_ms
            return nid, True, 'challenger'
    return cur, False, None


def prune(state):
    """Keep recent generations, the living population, the hall of fame and the champion's ancestry."""
    nodes, gen = state['nodes'], state['generation']
    keep = set(state['population']) | set(state['hall'])
    seen, stack = set(), [state['champion']] if state['champion'] else []
    while stack:                       # the champion and all its ancestors
        nid = stack.pop()
        if nid in seen or nid not in nodes:
            continue
        seen.add(nid); stack += nodes[nid]['parents']
    keep |= seen
    keep |= {nid for nid, n in nodes.items() if n['born'] > gen - KEEP_GENERATIONS}
    state['nodes'] = {nid: n for nid, n in nodes.items() if nid in keep}
