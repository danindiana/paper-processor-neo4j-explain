import os
import sys
import json
import time
import neo4j
import cudf
import cugraph
import pandas as pd

url = "bolt://localhost:7687"
user = "neo4j"
password = "password123"

print("Connecting to Neo4j...")
driver = neo4j.GraphDatabase.driver(url, auth=(user, password))

with driver.session() as session:
    print("Fetching edges for GPU layout...")
    res = session.run("""
        MATCH (s)-[r]->(t)
        RETURN id(s) as source, id(t) as target
    """)
    edges = [(r["source"], r["target"]) for r in res]
    
    if not edges:
        print("No edges found. Graph is empty.")
        sys.exit(0)
        
    print(f"Loaded {len(edges)} edges. Transloading to GPU VRAM...")
    
    pdf = pd.DataFrame(edges, columns=['source', 'target'])
    gdf = cudf.DataFrame(pdf)
    
    print("Building cuGraph topology...")
    G = cugraph.Graph()
    G.from_cudf_edgelist(gdf, source='source', destination='target', renumber=True)
    
    print("Running GPU ForceAtlas2 Physics Simulation...")
    t0 = time.time()
    pos_df = cugraph.force_atlas2(G, max_iter=500, strong_gravity_mode=False, outbound_attraction_distribution=True, lin_log_mode=False)
    t1 = time.time()
    print(f"GPU Layout computed in {t1-t0:.4f} seconds!")
    
    pos_pdf = pos_df.to_pandas()
    
    print("Pushing computed (x,y) coordinates back to Neo4j...")
    updates = pos_pdf.to_dict('records')
    
    session.run("""
        UNWIND $updates AS row
        MATCH (n) WHERE id(n) = row.vertex
        SET n.fx = row.x * 20, n.fy = row.y * 20
    """, updates=updates)
    print("Update complete! The frontend is now a static WebGL renderer.")
