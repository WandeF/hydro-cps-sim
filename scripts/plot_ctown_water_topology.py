#!/usr/bin/env python3
"""Render the hydraulic map directly from EPANET INP coordinates and links."""
from __future__ import annotations

import argparse
from collections import defaultdict
import hashlib
import json
import math

import yaml
from pathlib import Path

import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
from matplotlib.collections import LineCollection
from matplotlib.lines import Line2D
from matplotlib.legend_handler import HandlerBase
from matplotlib.offsetbox import AnnotationBbox, DrawingArea
from matplotlib.patches import Circle, Polygon, PathPatch, Rectangle, FancyArrowPatch
from matplotlib.path import Path as MplPath
from matplotlib.transforms import Affine2D, Bbox

ROOT = Path(__file__).resolve().parents[1]
NAME = 'ctown_water_topology'
INK = '#292D32'
PIPE = '#87939F'
BLUE = '#356FA4'
ORANGE = '#C47C29'


def symbol_artists(kind, color, transform):
    """Small vector hydraulic symbols; shared by the map and its legend."""
    artists = []

    def line(points, width=.9):
        path = MplPath(points, [MplPath.MOVETO] + [MplPath.LINETO]*(len(points)-1))
        artists.append(PathPatch(path, fill=False, edgecolor=color, linewidth=width,
                                 capstyle='round', joinstyle='round', transform=transform))

    def polygon(points, fill):
        artists.append(Polygon(points, closed=True, facecolor=fill, edgecolor=color,
                               linewidth=.85, joinstyle='round', transform=transform))

    if kind == 'tank':
        # Hide the underlying pipe inside the complete tank footprint, including
        # its open support frame. Incoming pipes meet the icon boundary.
        artists.append(Rectangle((-.79,-.77),1.58,1.63,facecolor='white',
                                 edgecolor='none',transform=transform))
        # Elevated tank: vessel with water, two supports and a short base.
        polygon([(-.78,.85), (.78,.85), (.78,.02), (-.78,.02)], 'white')
        polygon([(-.76,.35), (.76,.35), (.76,.04), (-.76,.04)], color)
        line([(-.42,.0), (-.42,-.74)])
        line([(.42,.0), (.42,-.74)])
        line([(-.67,-.76), (.67,-.76)])
    elif kind == 'reservoir':
        # Open reservoir basin with two horizontal water-surface strokes.
        # Open top distinguishes the basin from an elevated tank.
        artists.append(Polygon([(-.86,.66), (-.86,-.62), (.86,-.62), (.86,.66)],
                               closed=True, facecolor='white', edgecolor='none', transform=transform))
        line([(-.86,.66), (-.86,-.62), (.86,-.62), (.86,.66)])
        line([(-.63,.23), (.63,.23)])
        line([(-.47,-.06), (.47,-.06)])
    elif kind == 'pump':
        # Trace the supplied filled volute/neck silhouette as vector geometry.
        # Center the volute on the link midpoint (local origin), with the
        # outlet neck lower edge on the pipe axis y=0, pointing along local -X.
        # Match the neck height to the radius so its upper edge is tangent.
        line([(-1.,0), (1.,0)], width=1.)
        artists.append(Circle((0,0), .55, facecolor=color, edgecolor=color,
                              linewidth=.75, transform=transform))
        artists.append(Rectangle((-1.15,0),1.15,.55,facecolor=color,
                                 edgecolor=color,linewidth=.75,transform=transform))
    elif kind == 'valve':
        line([(-1.,0), (1.,0)])
        polygon([(-.77,-.56), (0,0), (-.77,.56)], color)
        polygon([(.77,-.56), (0,0), (.77,.56)], color)
    else:
        raise ValueError(kind)
    return artists


def add_symbol(ax, kind, xy, color, size, angle=0, device_id=None):
    box = DrawingArea(size, size, 0, 0)
    transform = Affine2D().rotate_deg(angle).scale(size/2).translate(size/2, size/2)
    for artist in symbol_artists(kind, color, transform):
        # DrawingArea adds its points-to-display transform when drawing.
        artist.set_transform(transform + box.get_transform())
        box.add_artist(artist)
    item = AnnotationBbox(box, xy, frameon=False, pad=0, box_alignment=(.5,.5),
                         xycoords='data', zorder=6)
    if device_id is not None:
        item.set_gid(f'hydraulic-device-{device_id}')
    ax.add_artist(item)


class HydraulicLegend(HandlerBase):
    def create_artists(self, legend, original, xdescent, ydescent, width, height, fontsize, trans):
        kind, color = original
        size = min(width, height*1.5)
        transform = Affine2D().scale(size/2).translate(width/2-xdescent, height/2-ydescent) + trans
        return symbol_artists(kind, color, transform)


def read_inp(path):
    sections = defaultdict(list)
    section = None
    for raw in path.read_text(encoding='utf-8-sig').splitlines():
        line = raw.split(';', 1)[0].strip()
        if not line:
            continue
        if line.startswith('['):
            section = line.upper()
        elif section is not None:
            sections[section].append(line.split())
    nodes, links, coordinates = {}, {}, {}
    for section, kind in [('[JUNCTIONS]', 'junction'), ('[RESERVOIRS]', 'reservoir'), ('[TANKS]', 'tank')]:
        for row in sections[section]:
            key = row[0]
            if key in nodes:
                raise ValueError(f'Duplicate node: {key}')
            nodes[key] = {'kind': kind}
    for section, kind in [('[PIPES]', 'pipe'), ('[PUMPS]', 'pump'), ('[VALVES]', 'valve')]:
        for row in sections[section]:
            key, start, end = row[:3]
            if key in links:
                raise ValueError(f'Duplicate link: {key}')
            if start not in nodes or end not in nodes:
                raise ValueError(f'Unknown endpoint: {row[:3]}')
            links[key] = {'kind': kind, 'start': start, 'end': end}
    for row in sections['[COORDINATES]']:
        key = row[0]
        if key in coordinates:
            raise ValueError(f'Duplicate coordinates: {key}')
        coordinates[key] = tuple(map(float, row[1:3]))
        if not all(math.isfinite(v) for v in coordinates[key]):
            raise ValueError(f'Invalid coordinates: {key}')
    if set(nodes) - coordinates.keys():
        raise ValueError(f'Missing coordinates: {set(nodes) - coordinates.keys()}')
    vertices = defaultdict(list)
    for row in sections['[VERTICES]']:
        if row[0] not in links:
            raise ValueError(f'Unknown vertex link: {row[0]}')
        xy = tuple(map(float, row[1:3]))
        if not all(math.isfinite(v) for v in xy):
            raise ValueError(f'Invalid vertex: {row}')
        vertices[row[0]].append(xy)
    for key, node in nodes.items():
        node['xy'] = coordinates[key]
    for key, link in links.items():
        link['path'] = [coordinates[link['start']], *vertices[key], coordinates[link['end']]]
    adjacency = defaultdict(set)
    for link in links.values():
        adjacency[link['start']].add(link['end'])
        adjacency[link['end']].add(link['start'])
    remaining = set(nodes)
    components = []
    while remaining:
        stack = [next(iter(remaining))]
        component = set()
        while stack:
            node = stack.pop()
            if node in component:
                continue
            component.add(node)
            stack.extend(adjacency[node] - component)
        remaining -= component
        components.append(len(component))
    return nodes, links, sorted(components, reverse=True)


def midpoint(path):
    lengths = [math.dist(a, b) for a, b in zip(path, path[1:])]
    target = sum(lengths) / 2
    for a, b, length in zip(path, path[1:], lengths):
        if target <= length and length:
            fraction = target / length
            return tuple(a[i] + fraction * (b[i] - a[i]) for i in (0, 1))
        target -= length
    return path[0]


def link_symbol_pose(path):
    """Mid-arc-length anchor and local Node1 -> Node2 direction in degrees."""
    lengths=[math.dist(a,b) for a,b in zip(path,path[1:])]
    distance=sum(lengths)/2
    for a,b,length in zip(path,path[1:],lengths):
        if length and distance <= length:
            fraction=distance/length
            xy=tuple(a[i]+fraction*(b[i]-a[i]) for i in (0,1))
            return xy,math.degrees(math.atan2(b[1]-a[1],b[0]-a[0]))
        distance-=length
    raise ValueError('Zero-length hydraulic link cannot define equipment orientation')


def convex_hull(points):
    points = sorted(set(points))
    def cross(o, a, b):
        return (a[0]-o[0])*(b[1]-o[1]) - (a[1]-o[1])*(b[0]-o[0])
    lower, upper = [], []
    for point in points:
        while len(lower) >= 2 and cross(lower[-2], lower[-1], point) <= 0:
            lower.pop()
        lower.append(point)
    for point in reversed(points):
        while len(upper) >= 2 and cross(upper[-2], upper[-1], point) <= 0:
            upper.pop()
        upper.append(point)
    return lower[:-1] + upper[:-1]


def build_regions(config, nodes, links):
    regions = {}
    for plc in config['plcs']:
        equipment = [s for s in plc['sensors'] if s in nodes and nodes[s]['kind'] == 'tank']
        equipment += plc['actuators']
        equipment += [s[:-1] for s in plc['sensors'] if s.endswith('F')
                      and s[:-1] in links and links[s[:-1]]['kind'] == 'valve']
        # R1 belongs to the source-site envelope; it is not a PLC actuator.
        if plc['name'] == 'PLC1':
            equipment.append('R1')
        equipment = list(dict.fromkeys(equipment))
        points = [tuple(nodes[s]['xy']) for s in plc['sensors'] if s in nodes]
        for key in equipment:
            points.extend([tuple(nodes[key]['xy'])] if key in nodes
                          else [tuple(xy) for xy in links[key]['path']])
        radius = {'PLC1':95, 'PLC2':250, 'PLC3':90, 'PLC4':75,
                  'PLC5':75, 'PLC6':105, 'PLC7':105, 'PLC8':250}[plc['name']]
        expanded = [(x + radius*math.cos(i*math.pi/18), y + radius*math.sin(i*math.pi/18))
                    for x,y in points for i in range(36)]
        extension_centers = {
            'PLC1':(-246010,147330), 'PLC2':(-249114,150170),
            'PLC8':(-247541,149200), 'PLC3':(-246700,148155),
            'PLC4':(-247900,149990), 'PLC5':(-245850,149350),
            'PLC6':(-247355,151475), 'PLC7':(-245745,150410),
        }
        if plc['name'] in extension_centers:
            ex,ey=extension_centers[plc['name']]
            # A quiet lobe in the requested direction accommodates a plain label.
            expanded += [(ex+dx,ey+dy) for dx,dy in [(-210,-115),(210,-115),(210,115),(-210,115)]]
        regions[plc['name']] = {'equipment':equipment, 'sensors':list(plc['sensors']), 'outline':convex_hull(expanded)}
    assigned = [key for region in regions.values() for key in region['equipment']]
    expected = {key for key,node in nodes.items() if node['kind'] != 'junction'} | {
                key for key,link in links.items() if link['kind'] != 'pipe'}
    assert len(assigned) == len(set(assigned)) and set(assigned) == expected
    assert set(regions) == {f'PLC{i}' for i in range(1,9)}
    return regions


def audit_sensors(config, nodes, links, regions):
    owners = {}
    audit = {}
    for plc in config['plcs']:
        name = plc['name']
        polygon = MplPath(regions[name]['outline'])
        categories = {'level':[], 'pressure':[], 'flow':[]}
        for tag in plc['sensors']:
            assert tag not in owners, f'Duplicate sensor ownership: {tag}'
            owners[tag] = name
            if tag in nodes:
                kind = 'level' if nodes[tag]['kind']=='tank' else 'pressure'
                categories[kind].append(tag)
                assert polygon.contains_point(nodes[tag]['xy']), (name,tag)
            else:
                assert tag.endswith('F') and tag[:-1] in links, f'Unknown sensor: {tag}'
                categories['flow'].append(tag)
                assert all(polygon.contains_point(xy) for xy in links[tag[:-1]]['path']), (name,tag)
        # Every pump/valve with a flow sensor also has both endpoint pressures.
        for tag in categories['flow']:
            link = links[tag[:-1]]
            assert link['start'] in plc['sensors'] and link['end'] in plc['sensors'], (name,tag)
        for actuator in plc['actuators']:
            assert actuator+'F' in plc['sensors'], (name,actuator)
        audit[name] = categories
    remote = sorted({(owners[c['dependant']], c['dependant'], plc['name'])
                     for plc in config['plcs'] for c in plc['controls']
                     if owners[c['dependant']] != plc['name']})
    assert {s for s in owners if s in nodes and nodes[s]['kind']=='tank'} == {
        k for k,n in nodes.items() if n['kind']=='tank'}
    return audit, remote


def draw_regions_and_scada(ax, regions, nodes, links, remote):
    preferred = {
        'PLC1':(-246010,147330), 'PLC2':(-249114,150170),
        'PLC3':(-246700,148155), 'PLC4':(-247900,149990),
        'PLC5':(-245850,149350), 'PLC6':(-247355,151475),
        'PLC7':(-245745,150410), 'PLC8':(-247541,149200),
    }
    fallback = {'PLC5':(-245795,149420), 'PLC4':(-247510,149920),
                'PLC1':(-245580,147510), 'PLC2':(-249125,150260),
                'PLC3':(-245745,148775), 'PLC6':(-247730,151300),
                'PLC7':(-245740,150620), 'PLC8':(-247900,149430)}
    fig=ax.figure
    fig.canvas.draw()
    renderer=fig.canvas.get_renderer()
    scale=fig.dpi/72
    node_pixels=ax.transData.transform([n['xy'] for n in nodes.values()])
    obstacles=[]
    for key,node in nodes.items():
        if node['kind']!='junction':
            x,y=ax.transData.transform(node['xy'])
            r=8.5*scale
            obstacles.append(Bbox.from_extents(x-r,y-r,x+r,y+r))
    for link in links.values():
        if link['kind']!='pipe':
            x,y=ax.transData.transform(midpoint(link['path']))
            r=7*scale
            obstacles.append(Bbox.from_extents(x-r,y-r,x+r,y+r))
    reserved=[]
    positions={}
    label_boxes=[]
    for name,region in regions.items():
        ax.add_patch(Polygon(region['outline'],closed=True,facecolor=BLUE,
                             edgecolor='none',alpha=.17,zorder=0))
        ax.add_patch(Polygon(region['outline'],closed=True,fill=False,
                             edgecolor=BLUE,linewidth=1.05,linestyle=(0,(4,3)),zorder=3))
        path=MplPath(region['outline'])
        xs,ys=zip(*region['outline'])
        dummy=ax.text(0,0,name,fontsize=9.5,fontweight='bold',ha='center',va='center')
        bbox=dummy.get_window_extent(renderer)
        half_w,half_h=bbox.width/2+3.3*scale,bbox.height/2+3.0*scale
        dummy.remove()
        candidates=[preferred[name]]+[(min(xs)+(max(xs)-min(xs))*i/26,
                                       min(ys)+(max(ys)-min(ys))*j/26)
                                      for i in range(1,26) for j in range(1,26)]
        best=None
        for xy in candidates:
            px,py=ax.transData.transform(xy)
            box=Bbox.from_extents(px-half_w,py-half_h,px+half_w,py+half_h)
            corners=ax.transData.inverted().transform([(box.x0,box.y0),(box.x1,box.y0),
                                                       (box.x0,box.y1),(box.x1,box.y1)])
            if not all(path.contains_point(corner) for corner in corners):
                continue
            if any(box.overlaps(other) for other in obstacles+label_boxes):
                continue
            hits=sum(box.contains(x,y) for x,y in node_pixels)
            score=hits*100000+math.dist(xy,preferred[name])
            if best is None or score<best[0]:
                best=(score,xy,box)
        if best is not None:
            _,xy,box=best
            label=ax.text(*xy,name,ha='center',va='center',fontsize=9.5,fontweight='bold',
                          color=BLUE,zorder=10)
            region['plc_label_inside']=True
        else:
            xy=fallback[name]
            anchor=min(region['outline'],key=lambda p:math.dist(p,xy))
            label=ax.annotate(name,anchor,xytext=xy,ha='center',va='center',fontsize=9.5,
                              fontweight='bold',color=BLUE,zorder=10,
                              arrowprops={'arrowstyle':'-','color':BLUE,'lw':.8})
            fig.canvas.draw()
            box=label.get_window_extent(renderer)
            region['plc_label_inside']=False
        positions[name]=xy
        region['plc_label_xy']=xy
        label_boxes.append(box)
        reserved.append(label)
    fig.canvas.draw()
    # Logical data dependency: tank sensor's owner -> consuming PLC.
    # Actual transport is SCADA-mediated; these are not added network links.
    radii={('PLC2','PLC1'):.48, ('PLC6','PLC4'):.36,
           ('PLC7','PLC5'):-.48, ('PLC8','PLC4'):-.28}
    for source,tank,target in remote:
        if (source,target)==('PLC2','PLC1'):
            sx,sy=positions[source]
            tx,ty=positions[target]
            vertices=[(sx-150,sy),(-249850,sy),(-249850,149850),(-249850,149400),
                      (-249850,148600),(-249850,146900),(-249350,146900),
                      (-248300,146900),(-246400,146900),(tx,ty-100)]
            path=MplPath(vertices,[MplPath.MOVETO]+[MplPath.CURVE4]*9)
            assert not any(path.intersects_path(MplPath(link['path']),filled=False)
                           for link in links.values()), 'T1 dependency curve crosses the water network'
            arrow=FancyArrowPatch(path=path,arrowstyle='-|>',mutation_scale=15,
                                  linewidth=1.4,linestyle=(0,(5,3)),color=BLUE,zorder=4)
            regions[target]['T1_dependency_curve_vertices']=vertices
        else:
            arrow=FancyArrowPatch(positions[source],positions[target],
                     connectionstyle=f'arc3,rad={radii[(source,target)]}',
                     arrowstyle='-|>',mutation_scale=15,linewidth=1.4,linestyle=(0,(5,3)),
                     color=BLUE,shrinkA=15,shrinkB=16,zorder=4)
        arrow.set_gid(f'dependent-{source}-{tank}-{target}')
        ax.add_patch(arrow)
    # SCADA in the top-right whitespace: only the icon and blue name remain.
    cx,cy=-245490,151330
    ax.add_patch(Rectangle((cx-130,cy-65),260,165,facecolor='white',edgecolor=BLUE,linewidth=1.4,zorder=5))
    ax.add_patch(Rectangle((cx-105,cy-42),210,115,facecolor=BLUE,alpha=.07,edgecolor='none',zorder=5))
    ax.plot([cx-22,cx-22,cx-80,cx+80,cx+22,cx+22],
            [cy-65,cy-108,cy-108,cy-108,cy-108,cy-65],color=BLUE,lw=1.3,zorder=5)
    reserved.append(ax.text(cx,cy-225,'SCADA Client',ha='center',va='center',
                            fontsize=13,fontweight='bold',color=BLUE,zorder=9))
    return reserved


def draw(nodes, links, output, config, hydraulic_only=False):
    plt.rcParams.update({'font.family': 'DejaVu Sans', 'font.size': 9,
                         'text.color': INK, 'svg.fonttype': 'none', 'pdf.fonttype': 42})
    fig, ax = plt.subplots(figsize=(8.2, 8.4))
    fig.subplots_adjust(left=.02, right=.98, bottom=.02, top=.98)
    points = [n['xy'] for n in nodes.values()]
    xmin, xmax = min(p[0] for p in points), max(p[0] for p in points)
    ymin, ymax = min(p[1] for p in points), max(p[1] for p in points)
    dx, dy = xmax-xmin, ymax-ymin
    if hydraulic_only:
        ax.set_xlim(xmin-.065*dx, xmax+.065*dx)
        ax.set_ylim(ymin-.065*dy, ymax+.065*dy)
    else:
        ax.set_xlim(-250025, xmax+.09*dx)
        ax.set_ylim(146680, ymax+.115*dy)
    ax.set_aspect('equal')
    ax.axis('off')
    regions, region_annotations = {}, []
    if not hydraulic_only:
        regions = build_regions(config, nodes, links)
        sensor_audit, remote = audit_sensors(config, nodes, links, regions)
        region_annotations = draw_regions_and_scada(ax, regions, nodes, links, remote)
    for kind, color, width in [('pipe', PIPE, .8), ('pump', ORANGE, 1.45), ('valve', ORANGE, 1.45)]:
        paths = [l['path'] for l in links.values() if l['kind'] == kind]
        ax.add_collection(LineCollection(paths, colors=color, linewidths=width, zorder=1 if kind == 'pipe' else 3))
    junctions = [n['xy'] for n in nodes.values() if n['kind'] == 'junction']
    ax.scatter(*zip(*junctions), s=7, c=INK, linewidths=0, zorder=2)
    pressure_tags=[tag for plc in config['plcs'] for tag in plc['sensors']
                   if tag in nodes and nodes[tag]['kind']=='junction']
    if not hydraulic_only:
        ax.scatter(*zip(*(nodes[tag]['xy'] for tag in pressure_tags)),s=25,
                   facecolors='white',edgecolors=BLUE,linewidths=.85,zorder=4)
    equipment = []
    for key, node in nodes.items():
        if node['kind'] == 'junction':
            continue
        add_symbol(ax, node['kind'], node['xy'], BLUE, 14 if node['kind'] == 'tank' else 16, device_id=key)
        equipment.append((key, node['xy'], BLUE, 10))
    for key, link in links.items():
        if link['kind'] == 'pipe':
            continue
        xy,angle = link_symbol_pose(link['path'])
        rotation = angle-180 if link['kind']=='pump' else angle
        link['symbol_angle_degrees'] = rotation
        link['nominal_direction_degrees'] = angle
        link['symbol_anchor'] = xy
        add_symbol(ax, link['kind'], xy, ORANGE, 11, angle=rotation, device_id=key)
        equipment.append((key, xy, INK, 8.2))
    counts = {kind: sum(n['kind'] == kind for n in nodes.values()) for kind in ['junction', 'reservoir', 'tank']}
    counts.update({kind: sum(l['kind'] == kind for l in links.values()) for kind in ['pipe', 'pump', 'valve']})
    legend = [Line2D([],[],color=PIPE,lw=1),
              Line2D([],[],ls='',marker='o',ms=3,color=INK),
              Line2D([],[],ls='',marker='o',ms=5,markerfacecolor='white',markeredgecolor=BLUE),
              ('reservoir',BLUE),('tank',BLUE),('pump',ORANGE),('valve',ORANGE),
              Line2D([],[],color=BLUE,lw=1.6,ls='--',marker='>',markevery=[1],ms=5)]
    if not hydraulic_only:
        ax.legend(handles=legend,labels=['Pipe','Junction','Pressure sensor','Reservoir','Tank','Pump','Valve','Dependent'],
                  handler_map={tuple:HydraulicLegend()},loc='upper left',bbox_to_anchor=(.015,.995),
                  ncol=1,frameon=False,handletextpad=.7,labelspacing=.6,fontsize=9)
    # Place labels in display space, penalizing collisions with labels and nodes.
    fig.canvas.draw()
    renderer = fig.canvas.get_renderer()
    node_pixels = ax.transData.transform(points)
    pipe_pixels = [MplPath(ax.transData.transform(link['path'])) for link in links.values()]
    symbol_bounds = []
    for key, xy, _, _ in equipment:
        size = (14 if nodes[key]['kind'] == 'tank' else 16) if key in nodes else 11
        px, py = ax.transData.transform(xy)
        radius = (size/2 + 2)*fig.dpi/72
        symbol_bounds.append(Bbox.from_extents(px-radius, py-radius, px+radius, py+radius))
    placed = [(a.get_bbox_patch().get_window_extent(renderer) if a.get_bbox_patch() is not None
               else a.get_window_extent(renderer)).expanded(1.15,1.2) for a in region_annotations]
    labels = []
    candidates = [(12,12),(12,-12),(-12,12),(-12,-12),(0,17),(0,-17),
                  (22,0),(-22,0),(22,22),(-22,22),(22,-22),(-22,-22),
                  (35,10),(-35,10),(35,-10),(-35,-10),(0,32),(0,-32)]
    for key, xy, color, size in ([] if hydraulic_only else equipment):
        best = None
        for ox, oy in candidates:
            annotation = ax.annotate(key, xy, xytext=(ox, oy), textcoords='offset points',
                                     ha='left' if ox>0 else 'right' if ox<0 else 'center',
                                     va='center', fontsize=size, color=color, fontweight='semibold')
            bbox = annotation.get_window_extent(renderer).expanded(1.14, 1.25)
            collisions = sum(bbox.overlaps(other) for other in placed)
            node_hits = sum(bbox.contains(x,y) for x,y in node_pixels)
            symbol_hits = sum(bbox.overlaps(bound) for bound in symbol_bounds)
            pipe_hits = sum(path.intersects_bbox(bbox,filled=False) for path in pipe_pixels)
            inside = ax.get_window_extent().contains(bbox.x0, bbox.y0) and ax.get_window_extent().contains(bbox.x1, bbox.y1)
            score = collisions*10000 + (not inside)*10000 + symbol_hits*10000 + node_hits*100 + pipe_hits*50 + math.hypot(ox,oy)
            annotation.remove()
            if best is None or score < best[0]:
                best = (score, ox, oy, bbox)
        _, ox, oy, bbox = best
        placed.append(bbox)
        label = ax.annotate(key, xy, xytext=(ox,oy), textcoords='offset points',
                    ha='left' if ox>0 else 'right' if ox<0 else 'center', va='center',
                    fontsize=size, color=color, fontweight='semibold', zorder=8,
                    arrowprops={'arrowstyle':'-', 'color':'#68717A', 'lw':.55,
                                'shrinkA':2.5, 'shrinkB':5})
        labels.append(key)
    assert len(labels) == (0 if hydraulic_only else counts['reservoir'] + counts['tank'] + counts['pump'] + counts['valve'])
    figure_name = NAME + ('_pure' if hydraulic_only else '')
    for fmt in ['pdf', 'svg', 'png']:
        folder = output / fmt
        folder.mkdir(parents=True, exist_ok=True)
        fig.savefig(folder / f'{figure_name}.{fmt}', dpi=600, facecolor='white')
    fig.savefig(output / f'{figure_name}_preview.png', dpi=160, facecolor='white')
    plt.close(fig)
    return counts, labels, regions


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--input', type=Path, help='Defaults to the INP referenced by --config')
    parser.add_argument('--config', type=Path, default=ROOT/'examples/C_Town/config.yaml')
    parser.add_argument('--output', type=Path, default=ROOT/'figures')
    parser.add_argument('--hydraulic-only', action='store_true', help='Export a separate pure water-network figure without PLC/SCADA/legend overlays')
    args = parser.parse_args()
    config = yaml.safe_load(args.config.read_text())
    if args.input is None:
        args.input = Path(config['inp_file'])
        if not args.input.is_absolute():
            args.input = args.config.parent / args.input
    nodes, links, components = read_inp(args.input)
    counts, labels, regions = draw(nodes, links, args.output, config, args.hydraulic_only)
    owners = {s:p['name'] for p in config['plcs'] for s in p['sensors']}
    remote_dependencies = sorted({(owners[c['dependant']], c['dependant'], p['name'])
                                  for p in config['plcs'] for c in p['controls']
                                  if owners[c['dependant']] != p['name']})
    assert remote_dependencies == [('PLC2','T1','PLC1'), ('PLC6','T5','PLC4'),
                                   ('PLC7','T4','PLC5'), ('PLC8','T7','PLC4')]
    manifest = {'source': str(args.input.resolve()), 'source_sha256': hashlib.sha256(args.input.read_bytes()).hexdigest(),
                'config': str(args.config.resolve()), 'config_sha256': hashlib.sha256(args.config.read_bytes()).hexdigest(),
                'sensor_audit': audit_sensors(config,nodes,links,regions)[0] if regions else {},
                'dependency_direction': 'Sensor-owning PLC to consuming PLC; logical SCADA-mediated dependency, not direct network wiring.',
                'plc_regions': regions, 'remote_dependencies': remote_dependencies if regions else [], 'region_policy': 'Schematic local equipment/telemetry ownership, not hydraulic district boundaries. PLC labels are schematic positions.',
                'scada': 'Remote monitoring system serving PLC1-PLC8 over Modbus TCP.' if regions else None,
                'counts': counts, 'connected_component_sizes': components, 'equipment_labels': labels,
                'nodes': nodes, 'links': links, 'coordinate_policy': 'Original INP coordinates and vertices, equal aspect ratio.',
                'symbols': {'reservoir': 'Open basin with water lines', 'tank': 'Elevated tank on supports',
                            'pump': 'Reference-style filled volute silhouette with outlet neck along INP Node1-to-Node2 direction', 'valve': 'Opposing triangles (bow tie)'},
                'palette': {'junction': INK, 'pipe': PIPE, 'reservoir_tank': BLUE, 'pump_valve': ORANGE},
                'note': 'All links are included regardless of initial status. Pump outlet follows INP Node1-to-Node2 nominal direction; this does not represent simulated instantaneous flow. Pure export has no visible text.'}
    figure_name = NAME + ('_pure' if args.hydraulic_only else '')
    (args.output/f'{figure_name}_manifest.json').write_text(json.dumps(manifest, indent=2)+'\n')
    print(json.dumps({'counts':counts, 'connected_component_sizes':components, 'output':str(args.output)}, indent=2))


if __name__ == '__main__':
    main()
