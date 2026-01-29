# Weight (look back 14 days + extend to tomorrow for UTC coverage)
    try:
        start_date = (date.fromisoformat(target_date) - timedelta(days=14)).isoformat()
        end_date = (date.fromisoformat(target_date) + timedelta(days=1)).isoformat()
        
        weight_data = client.get_body_composition(start_date, end_date)
        print(f"   Weight raw type: {type(weight_data)}")
        
        if weight_data and isinstance(weight_data, dict):
            weights = weight_data.get("dateWeightList", []) or weight_data.get("weightList", [])
            
            if weights and len(weights) > 0:
                print(f"   DEBUG: Found {len(weights)} weight entries")
                
                # Fonction de tri robuste (retourne tuple pour éviter int vs str)
                def sort_key(w):
                    for k in ("timestampGMT", "timestampLocal", "gmtTimestamp"):
                        v = w.get(k)
                        if isinstance(v, (int, float)):
                            return (1, int(v))  # 1 = priorité timestamp
                    for k in ("calendarDate", "date"):
                        v = w.get(k)
                        if isinstance(v, str) and len(v) >= 10:
                            return (0, v[:10])  # 0 = fallback date string
                    return (0, "0000-00-00")
                
                # Filtrer les entrées valides (poids > 0)
                weights_valid = [w for w in weights if (w.get("weight") or 0) > 0]
                
                # Debug: afficher toutes les entrées avec leur clé de tri
                for i, w in enumerate(weights_valid):
                    w_kg = w.get("weight", 0) / 1000
                    w_date = w.get("calendarDate", w.get("date", "unknown"))
                    w_ts = w.get("timestampGMT", w.get("gmtTimestamp", "N/A"))
                    print(f"      [{i}] KEY={sort_key(w)} | {w_date} | ts={w_ts} | {w_kg} kg")
                
                if weights_valid:
                    weights_sorted = sorted(weights_valid, key=sort_key, reverse=True)
                    latest = weights_sorted[0]
                    weight_grams = latest.get("weight", 0)
                    
                    results["weight"] = round(weight_grams / 1000, 1)
                    latest_date = latest.get("calendarDate", "unknown")
                    latest_ts = latest.get("timestampGMT", latest.get("gmtTimestamp", "N/A"))
                    print(f"   DEBUG: Selected = {latest_date} (ts={latest_ts}) = {results['weight']} kg")
                    
                    if len(weights_sorted) > 1:
                        oldest = weights_sorted[-1]
                        first_weight = oldest.get("weight", 0) / 1000
                        results["weightChange"] = round(results["weight"] - first_weight, 1)
                else:
                    print("   ⚠️ No valid weight entries (all have weight=0)")
        
        print(f"   ✓ Weight: {results['weight']} kg (change: {results['weightChange']})")
    except Exception as e:
        print(f"   ❌ Weight error: {e}")
