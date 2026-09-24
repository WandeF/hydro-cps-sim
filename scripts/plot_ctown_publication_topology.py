#!/usr/bin/env python3
"""Reproduce the supplied annotated layout with original INP vector geometry."""
from __future__ import annotations

import argparse
import hashlib
import json
import math
from pathlib import Path

import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import yaml
from matplotlib.collections import LineCollection
from matplotlib.patches import Circle, Ellipse, FancyArrowPatch, PathPatch, Polygon, Rectangle
from matplotlib.path import Path as MplPath

from plot_ctown_water_topology import (
    BLUE, INK, ORANGE, PIPE, ROOT, add_symbol, audit_sensors, link_symbol_pose, read_inp,
)

NAME = 'ctown_water_topology_annotated'
REGION = '#087FA5'
PLC_RED = '#F1262D'
SCADA = '#0065AF'
ATTACK = '#D71920'
HEIGHT = 1600
SCALE = 1012 / 2953.82


def page(x, y):
    """Reference-layout pixels to upright, equal-aspect drawing coordinates."""
    return (x, HEIGHT-y)


def project(xy):
    x, y = xy
    return page(172 + SCALE*(x+249114.17), 600-SCALE*(y-149950.9))


# Hand-traced layout envelopes from the user reference; these are schematic
# ownership areas, not hydraulic districts or surveyed PLC cabinet positions.
ENVELOPES = {
    'PLC1': [(1137,1245),(1308,1245),(1390,1338),(1310,1428),(1137,1428)],
    'PLC2': [(147,495),(201,502),(207,584),(200,621),(181,634),(149,620),(132,591),(134,537)],
    'PLC3': [(935,908),(985,891),(1073,927),(1134,1159),(1115,1175),(838,1125),(827,1104)],
    'PLC4': [(677,607),(721,590),(760,589),(804,595),(844,619),(894,674),(929,733),
             (960,803),(940,875),(913,925),(884,937),(867,924),(856,851),(826,783),
             (793,708),(736,684),(694,676),(675,663)],
    'PLC5': [(1026,744),(1129,784),(1134,875),(1077,888),(964,869),(964,840)],
    'PLC6': [(570,211),(666,173),(828,124),(975,159),(992,197),(936,219),
             (891,204),(791,214),(744,310),(672,323),(621,300),(557,263)],
    'PLC7': [(1172,283),(1245,277),(1257,654),(1205,665),(1128,544),(1127,454)],
    'PLC8': [(700,725),(744,726),(775,739),(796,756),(806,779),(802,823),
             (756,827),(712,819),(681,803),(676,744)],
}
CABINETS = {'PLC1':(1265,1389),'PLC2':(167,528),'PLC3':(980,1110),
            'PLC4':(709,642),'PLC5':(1086,854),'PLC6':(610,244),
            'PLC7':(1206,410),'PLC8':(760,786)}
PLC_LABELS = {'PLC1':(1232,1457),'PLC2':(172,479),'PLC3':(980,1173),
              'PLC4':(627,637),'PLC5':(1177,851),'PLC6':(508,232),
              'PLC7':(1290,416),'PLC8':(632,781)}
# Device captions follow the reference's open spaces, with no label boxes.
LINK_LABELS = {
    'PU1':(1192,1268,0), 'PU2':(1192,1318,0), 'PU3':(1192,1363,0),
    'PU4':(1050,957,-25), 'PU5':(979,920,-25),
    'PU6':(1019,829,-30), 'PU7':(1091,807,-25),
    'PU8':(763,677,-47), 'PU9':(811,652,-44),
    'PU10':(920,800,-22), 'PU11':(890,736,-20),
    'v1':(1209,590,-55), 'V45':(680,199,-9),
    'V47':(896,163,-23), 'V2':(885,1097,-28),
}


def plc_symbol(ax, x, y, size=41, gid=None):
    x,y=page(x,y)
    s=size/41
    box=Rectangle((x-size/2,y-size/2),size,size,facecolor='white',
                  edgecolor=PLC_RED,lw=1.2,zorder=7)
    box.set_gid(gid)
    ax.add_patch(box)
    ax.add_patch(Rectangle((x-14*s,y-7*s),12*s,21*s,fill=False,edgecolor=PLC_RED,lw=.9,zorder=8))
    ax.plot([x-11*s,x-5*s],[y+9*s,y+9*s],c=PLC_RED,lw=.8,zorder=8)
    ax.plot([x-11*s,x-5*s],[y-3*s,y-3*s],c=PLC_RED,lw=.8,zorder=8)
    for dx,dy in [(4,12),(12,12),(4,5),(12,5),(-12,-13),(-4,-13),(4,-13),(12,-13)]:
        ax.add_patch(Circle((x+dx*s,y+dy*s),1.35*s,color=PLC_RED,zorder=8))


def text(ax,x,y,value,**kwargs):
    return ax.text(*page(x,y),value,ha=kwargs.pop('ha','center'),va='center',
                   fontsize=kwargs.pop('fontsize',12),zorder=10,**kwargs)


def arrow(ax, vertices, gid):
    assert all((a[0]==b[0]) != (a[1]==b[1]) for a,b in zip(vertices,vertices[1:])), 'Dependency segments must be horizontal or vertical'
    path=MplPath([page(*v) for v in vertices],[MplPath.MOVETO]+[MplPath.LINETO]*(len(vertices)-1))
    patch=FancyArrowPatch(path=path,arrowstyle='-|>',mutation_scale=15,
                          color='#111111',lw=1.6,joinstyle='miter',zorder=5)
    patch.set_gid(gid)
    ax.add_patch(patch)
    return path


def draw_legend(ax):
    rows=[('Pipe',65),('Junction',103),('Pump',143),('Valve',183),('Tank',228),
          ('Reservoir',273),('PLC',318),('PLC control area',365),
          ('Sensor–actuator\nlogical dependency',419)]
    for label,y in rows:
        text(ax,134,y,label,ha='left',fontsize=10.8,color='#111111',linespacing=1.2)
    ax.plot([53,111],[HEIGHT-65]*2,c=PIPE,lw=.8)
    ax.scatter(*page(82,103),s=10,c=INK)
    for kind,y,color in [('pump',143,ORANGE),('valve',183,ORANGE),
                         ('tank',228,BLUE),('reservoir',273,BLUE)]:
        add_symbol(ax,kind,page(82,y),color,17 if kind!='reservoir' else 19)
    plc_symbol(ax,82,318,36)
    ax.add_patch(Rectangle(page(52,382),60,34,facecolor=REGION+'14',
                           edgecolor=REGION,lw=1.3,linestyle=(0,(4,3))))
    # Straight legend arrow; map dependency routes use orthogonal polylines.
    line=FancyArrowPatch(page(51,419),page(113,419),arrowstyle='-|>',
                          mutation_scale=14,color='#111111',lw=1.5)
    line.set_gid('legend-straight-logical-dependency')
    ax.add_patch(line)


def draw_scada(ax):
    cx=1383
    text(ax,cx,67,'SCADA Client',fontsize=13,color=SCADA)
    ax.add_patch(Rectangle(page(cx-53,160),106,68,facecolor='white',edgecolor=SCADA,lw=1.7,zorder=7))
    ax.plot([cx-53,cx+53],[HEIGHT-149]*2,c=SCADA,lw=1.5,zorder=8)
    ax.plot([cx-8,cx-8,cx-22,cx+22,cx+8,cx+8],
            [HEIGHT-y for y in (161,174,181,181,174,161)],c=SCADA,lw=1.5)
    patch=FancyArrowPatch(page(cx,193),page(cx,294),arrowstyle='<|-|>',
                          mutation_scale=14,color=SCADA,lw=1.4,linestyle=(0,(4,3)))
    patch.set_gid('plc-scada-bidirectional-communication')
    ax.add_patch(patch)
    text(ax,cx+23,244,'Modbus TCP\ncommunication',ha='left',fontsize=8.8,color=SCADA,linespacing=1.2)
    text(ax,cx,319,'PLC1–PLC8',fontsize=12,color=SCADA)


def attack_symbol(ax, kind, center, size=46, gid=None):
    """Reference-inspired red vector icons, in local coordinates facing up."""
    cx,cy=page(*center)
    scale=size/2

    def xy(p):
        return (cx+p[0]*scale,cy+p[1]*scale)

    def stroke(points, codes=None, fill=False):
        path=MplPath([xy(p) for p in points],codes or [MplPath.MOVETO]+[MplPath.LINETO]*(len(points)-1))
        item=PathPatch(path,facecolor='white' if fill else 'none',edgecolor=ATTACK,
                       lw=1.4,joinstyle='round',capstyle='round',zorder=15)
        ax.add_patch(item)
        return item

    if kind=='mitm':
        # Hooded figure with a face and laptop, matching the supplied reference.
        outer=[(-.85,-.70),(-.93,-.12),(-.67,.05),(-.57,.17),
               (-.32,.86),(-.18,1.04),(0,1.05),
               (.18,1.04),(.32,.86),(.57,.17),
               (.67,.05),(.93,-.12),(.85,-.70),(-.85,-.70)]
        body=stroke(outer,[MplPath.MOVETO]+[MplPath.CURVE4]*12+[MplPath.CLOSEPOLY],fill=True)
        body.set_zorder(13)
        face=Ellipse(xy((0,.30)),.73*scale,.87*scale,facecolor='white',edgecolor=ATTACK,lw=1.25,zorder=14)
        ax.add_patch(face)
        for ex in (-.13,.13):
            ax.add_patch(Circle(xy((ex,.40)),.035*scale,color=ATTACK,zorder=15))
        ax.add_patch(Rectangle(xy((-.57,-.73)),1.14*scale,.65*scale,
                               facecolor='white',edgecolor=ATTACK,lw=1.4,zorder=15))
        ax.add_patch(Circle(xy((0,-.41)),.075*scale,color=ATTACK,zorder=16))
    elif kind=='dos':
        body=stroke([(-.85,-.35),(-.53,-.06),(-.96,.35),(-.45,.24),
                     (-.60,.85),(-.12,.51),(.09,1.05),(.28,.47),
                     (.80,.83),(.61,.24),(1.,.33),(.58,-.34)],fill=True)
        stroke([(-.98,-.72),(-.79,-.42),(.80,-.42),(.98,-.72),(-.98,-.72)],fill=True)
        stroke([(-.58,-.70),(-.28,-.70)])
        stroke([(.08,-.70),(.56,-.70)])
    elif kind=='injection':
        for sign in (-1,1):
            for y,dy in ((.25,.21),(-.03,0),(-.31,-.21)):
                stroke([(sign*.39,y),(sign*.71,y+dy),(sign*.83,y+dy)])
            stroke([(sign*.18,.55),(sign*.32,.87)])
        body=Ellipse(xy((0,-.05)),.88*scale,1.18*scale,facecolor='white',edgecolor=ATTACK,lw=1.4,zorder=14)
        ax.add_patch(body)
        ax.add_patch(Ellipse(xy((0,.57)),.52*scale,.35*scale,facecolor='white',edgecolor=ATTACK,lw=1.4,zorder=14))
        stroke([(0,.38),(0,-.56)])
        for x in (-.20,.20):
            stroke([(x,.15),(x,-.34)])
    else:
        raise ValueError(kind)
    body.set_gid(gid)


def route_middle(points):
    """Locate the half-length point along the displayed orthogonal polyline."""
    lengths=[math.dist(a,b) for a,b in zip(points,points[1:])]
    remaining=sum(lengths)/2
    for a,b,length in zip(points,points[1:],lengths):
        if remaining<=length:
            return tuple(a[i]+(b[i]-a[i])*remaining/length for i in (0,1))
        remaining-=length
    return points[-1]


def draw_attacks(ax, routes, plot_links, config):
    plc4=next(p for p in config['plcs'] if p['name']=='PLC4')
    close_rule=next(c for c in plc4['controls'] if c['actuator']=='PU10' and c['action']=='closed')
    assert close_rule['dependant']=='T7' and close_rule['type']=='above' and close_rule['value']==4.8
    positions={'mitm':route_middle(routes['PLC8','PLC4']),
               'dos':route_middle(routes['PLC7','PLC5']), 'injection':(959,756)}
    for kind,position in positions.items():
        attack_symbol(ax,kind,position,size=46 if kind!='dos' else 50,gid=f'attack-map-{kind}')
    # Short pointer identifies PU10 without covering the pump silhouette.
    pump,_=link_symbol_pose(plot_links['PU10']['path'])
    ax.plot([page(942,761)[0],pump[0]+10],[page(942,761)[1],pump[1]],
            c=ATTACK,lw=.85,zorder=12)

    # A compact bottom legend keeps the original hydraulic and SCADA layout.
    ax.plot([53,1504],[HEIGHT-1535]*2,color=ATTACK,alpha=.35,lw=.8)
    entries=[
        ('mitm',78,132,'MITM',
         'SCADA → PLC4\nModbus TCP downlink tampering'),
        ('dos',583,637,'DoS',
         'Bots in 3 independent LANs compete\nfor a 10 Mbps bottleneck toward PLC7'),
        ('injection',1083,1137,'PLC logic injection',
         'PLC4 / PU10: modify T7 high-level\npump-off threshold (baseline: 4.8 m)'),
    ]
    for kind,ix,tx,title,detail in entries:
        attack_symbol(ax,kind,(ix,1594),size=45,gid=f'attack-legend-{kind}')
        text(ax,tx,1570,title,ha='left',fontsize=11.5,color=ATTACK,fontweight='semibold')
        text(ax,tx,1614,detail,ha='left',fontsize=8.8,color=INK,linespacing=1.4)
    return {'positions':positions,
            'mitm':{'target':'SCADA → PLC4 Modbus TCP downlink','map_marker':'PLC8 → PLC4 dependency polyline midpoint'},
            'dos':{'bots':'3 independent LANs','bottleneck_mbps':10,'direction':'toward PLC7','map_marker':'PLC7 → PLC5 dependency polyline midpoint'},
            'injection':{'plc':'PLC4','actuator':'PU10','sensor':'T7','original_close_threshold_m':4.8,'replacement_threshold_m':None},
            'note':'Attack markers show scenario locations; black polylines remain logical dependencies, not physical attack paths. Simulation configuration is unchanged.'}


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--attacks',action='store_true',help='Add the three attack markers and their legend.')
    args=parser.parse_args()
    output_name=NAME+('_attacks' if args.attacks else '')
    config_path=ROOT/'examples/C_Town/config.yaml'
    source=ROOT/'examples/C_Town/ctown_map.inp'
    config=yaml.safe_load(config_path.read_text())
    nodes,links,components=read_inp(source)
    # Check envelope containment in the same coordinates as the displayed map.
    plot_nodes={key:{**node,'xy':project(node['xy'])} for key,node in nodes.items()}
    plot_links={key:{**link,'path':[project(p) for p in link['path']]} for key,link in links.items()}
    regions={name:{'outline':[page(*v) for v in vertices]} for name,vertices in ENVELOPES.items()}
    audit,remote=audit_sensors(config,plot_nodes,plot_links,regions)

    plt.rcParams.update({'font.family':'DejaVu Sans','svg.fonttype':'none','pdf.fonttype':42})
    extra_height=70 if args.attacks else 0
    fig=plt.figure(figsize=(10.4,10.4*(HEIGHT+extra_height)/1561))
    ax=fig.add_axes((0,0,1,1))
    ax.set(xlim=(0,1561),ylim=(-extra_height,HEIGHT),aspect='equal')
    ax.axis('off')
    for name,region in regions.items():
        patch=Polygon(region['outline'],facecolor=REGION+'14',edgecolor=REGION,
                       linewidth=1.9,linestyle=(0,(4,3)),joinstyle='round',zorder=0)
        patch.set_gid(f'control-area-{name}')
        ax.add_patch(patch)
    for kind,color,width in [('pipe',PIPE,.8),('pump',ORANGE,1.4),('valve',ORANGE,1.4)]:
        collection=LineCollection([l['path'] for l in plot_links.values() if l['kind']==kind],
                                  colors=color,linewidths=width,zorder=1)
        collection.set_gid(f'hydraulic-links-{kind}')
        ax.add_collection(collection)
    junctions=[n['xy'] for n in plot_nodes.values() if n['kind']=='junction']
    ax.scatter(*zip(*junctions),s=7,c=INK,linewidths=0,zorder=2)
    tank_label_offsets={'T1':(0,30),'T2':(0,-37),'T3':(0,-37),
                        'T4':(39,0),'T5':(-35,0),'T6':(0,-40),'T7':(0,29),'R1':(56,0)}
    for key,node in plot_nodes.items():
        if node['kind']=='junction':continue
        add_symbol(ax,node['kind'],node['xy'],BLUE,14 if node['kind']=='tank' else 17,device_id=key)
        x,y=node['xy'];dx,dy=tank_label_offsets[key]
        ax.text(x+dx,y+dy,key,color=BLUE,fontsize=13,ha='center',va='center',zorder=9)
    for key,link in plot_links.items():
        if link['kind']=='pipe':continue
        xy,angle=link_symbol_pose(link['path'])
        rotation=angle-180 if link['kind']=='pump' else angle
        add_symbol(ax,link['kind'],xy,ORANGE,11,angle=rotation,device_id=key)
        x,y,caption_angle=LINK_LABELS[key]
        text(ax,x,y,key,color=ORANGE,fontsize=11,rotation=caption_angle,
             rotation_mode='anchor')
    for name,(x,y) in CABINETS.items():
        plc_symbol(ax,x,y,gid=f'plc-cabinet-{name}')
        text(ax,*PLC_LABELS[name],name,fontsize=13,color='#111111')

    routes={
        ('PLC2','PLC1'):[(134,636),(66,636),(66,1495),(1232,1495),(1232,1474)],
        ('PLC6','PLC4'):[(506,253),(506,637),(588,637)],
        ('PLC7','PLC5'):[(1291,439),(1370,439),(1370,846),(1220,846)],
        ('PLC8','PLC4'):[(645,756),(627,756),(627,658)],
    }
    for start,tank,end in remote:
        path=arrow(ax,routes[start,end],f'dependency-{start}-{tank}-{end}')
        if start=='PLC2':
            assert not any(path.intersects_path(MplPath(link['path']),filled=False)
                           for link in plot_links.values()), 'Outer dependency crosses hydraulic links'
    draw_legend(ax)
    draw_scada(ax)
    attacks=draw_attacks(ax,routes,plot_links,config) if args.attacks else None
    out=ROOT/'figures'
    for ext in ('svg','pdf','png'):
        (out/ext).mkdir(exist_ok=True,parents=True)
        fig.savefig(out/ext/f'{output_name}.{ext}',dpi=600,facecolor='white')
    fig.savefig(out/f'{output_name}_preview.png',dpi=160,facecolor='white')
    plt.close(fig)
    manifest={
        'source':str(source),'source_sha256':hashlib.sha256(source.read_bytes()).hexdigest(),
        'config':str(config_path),'config_sha256':hashlib.sha256(config_path.read_bytes()).hexdigest(),
        'node_count':len(nodes),'link_count':len(links),'components':components,
        'sensor_audit':audit,'remote_dependencies':remote,
        'region_outlines':ENVELOPES,'plc_cabinets':CABINETS,
        'legend_dependency_arrow':'straight','map_dependency_arrows':'orthogonal polylines',
        'dependency_routes':{f'{start}->{end}':vertices for (start,end),vertices in routes.items()},
        'plc_scada_protocol':'Modbus TCP',
        'attacks':attacks,
        'coordinate_policy':'Original INP geometry under a uniform scale and translation.',
        'label_correction':'T2 and T3 use INP IDs; their labels are reversed in the supplied reference.',
        'note':'Control areas and PLC cabinet positions are schematic. Dependency arrows denote sensor-to-actuator logic, carried via SCADA.',
    }
    (out/f'{output_name}_manifest.json').write_text(json.dumps(manifest,indent=2)+'\n')
    print(json.dumps({'nodes':len(nodes),'links':len(links),'remote':remote,'output':str(out/output_name)},indent=2))


if __name__=='__main__':
    main()
