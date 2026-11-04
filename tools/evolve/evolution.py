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


def choose_champion(state):
    """Among strategies that passed the out-of-sample gate, the best training fitness takes over paper trading.
    Returns (champion_id, switched)."""
    nodes, cur = state['nodes'], state['champion']
    if not state['hall']:
        return cur, False
    best = state['hall'][0]
    if cur is None or cur not in nodes or not nodes[cur]['qualified']:
        state['champion'] = best
        return best, best != cur
    if best != cur and nodes[best]['fitness'] > max(nodes[cur]['fitness'] * SWITCH_MARGIN, nodes[cur]['fitness'] + .01):
        state['champion'] = best
        return best, True
    return cur, False


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
