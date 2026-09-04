import discord
from discord import app_commands
from discord.ext import commands, tasks
import json
import os
from datetime import datetime, timedelta
import logging
import requests
import io 
import psutil
import platform
import time
import shutil
from zoneinfo import ZoneInfo


class DiscordWebhookHandler(logging.Handler):
    def emit(self, record):
        try:
            # Formatiert den Log-Eintrag
            log_entry = self.format(record)
            
            # Discord-Nachrichten dürfen maximal 2000 Zeichen lang sein
            if len(log_entry) > 1900:
                log_entry = log_entry[:1900] + "\n... [Abgeschnitten]"
            
            # Baut die Nachricht mit schicker Code-Block-Formatierung
            payload = {
                "content": f"```ini\n[{record.levelname}] {record.name}\n{log_entry}\n```"
            }
            
            # Sendet den Log an den Channel
            requests.post(LOG_WEBHOOK_URL, json=payload, timeout=2)
        except Exception:
            pass # Verhindert Abstürze des Bots, falls Discord mal kurz offline ist

BOT_START_ZEIT = time.time()


# Logging-System von Discord.py abfangen
logger = logging.getLogger('discord')
logger.setLevel(logging.INFO) # INFO zeigt alles an, WARNING zeigt nur Fehler

# Den Webhook als Ziel für die Logs hinzufügen
webhook_handler = DiscordWebhookHandler()
webhook_handler.setFormatter(logging.Formatter('%(asctime)s - %(message)s', "%Y-%m-%d %H:%M:%S"))
logger.addHandler(webhook_handler)

intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)

WOCHENTAGE = ["Montag", "Dienstag", "Mittwoch", "Donnerstag", "Freitag", "Samstag", "Sonntag"]

ROLLEN_PRIORITAET = {
    "Heiler": 5, "Tank": 10, "DPS": 1
}

RAID_ROLLE_NAME = "Raidgruppe"

def hat_raid_rolle(interaction: discord.Interaction, raid_id: str):
    erlaubte_rolle = hole_einstellung(interaction.guild_id, raid_id, "raid_rolle_name", "Raidgruppe")
    return any(role.name == erlaubte_rolle for role in interaction.user.roles)

ROLLEN_EMOJIS = {
    "Heiler": "<:Heiler:1509966781635366973>", 
    "Tank": "<:Tank:1509966756641374218>",  
    "DPS": "<:DPS:1509966802778591262>"   
}

STANDARD_EMOJI = "<:MSQ:1510567044137881711>"

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

DATEINAME_ZEITEN = os.path.join(BASE_DIR, "schnellzeiten.json")
DATEINAME_DATEN = os.path.join(BASE_DIR, "planung_daten.json")
DATEINAME_CONFIG = os.path.join(BASE_DIR, "planung_config.json")

# DATEINAME_EINSTELLUNGEN und server_einstellungen wurden gelöscht!
planung_daten = {}
user_schnellzeiten = {}
planung_config = {} # Wird ab sofort komplett dynamisch gefüllt

def hole_einstellung(guild_id, raid_id, schluessel, default_wert):
    """Holt eine Einstellung für einen spezifischen Raid."""
    g_str = str(guild_id)
    if g_str in planung_config and raid_id in planung_config[g_str]:
        return planung_config[g_str][raid_id].get(schluessel, default_wert)
    return default_wert

user_cooldowns = {}

def ist_auf_cooldown(user_id, user_name="Unbekannt"):
    jetzt = datetime.now()
    if user_id in user_cooldowns:
        if (jetzt - user_cooldowns[user_id]).total_seconds() < 3:
            # NEU: Logge den Spamschutz-Treffer
            logger.warning(f"⚠️ [SPAMSCHUTZ] {user_name} (ID: {user_id}) hat den Spamschutz ausgelöst.")
            return True
            
    user_cooldowns[user_id] = jetzt
    return False

def speichere_alles():
    with open(DATEINAME_ZEITEN, "w", encoding="utf-8") as f:
        json.dump(user_schnellzeiten, f, indent=4)
    with open(DATEINAME_DATEN, "w", encoding="utf-8") as f:
        json.dump(planung_daten, f, indent=4)
    with open(DATEINAME_CONFIG, "w", encoding="utf-8") as f:
        json.dump(planung_config, f, indent=4)

def lade_alles():
    global user_schnellzeiten, planung_daten, planung_config
    if os.path.exists(DATEINAME_ZEITEN):
        try:
            with open(DATEINAME_ZEITEN, "r", encoding="utf-8") as f:
                user_schnellzeiten = {int(k): v for k, v in json.load(f).items()}
        except json.JSONDecodeError: user_schnellzeiten = {}
            
    if os.path.exists(DATEINAME_DATEN):
        try:
            with open(DATEINAME_DATEN, "r", encoding="utf-8") as f:
                planung_daten = json.load(f)
        except json.JSONDecodeError: planung_daten = {}
            
    if os.path.exists(DATEINAME_CONFIG):
        try:
            with open(DATEINAME_CONFIG, "r", encoding="utf-8") as f:
                planung_config = json.load(f)
        except json.JSONDecodeError: planung_config = {}

def hole_wochen_daten(start_datum_str):
    try:
        teile = start_datum_str.strip().split('.')
        tag, monat = int(teile[0]), int(teile[1])
        jahr = int(teile[2]) if len(teile) > 2 and teile[2] else datetime.now().year
        start_date = datetime(jahr, monat, tag)
        return {tag_name: (start_date + timedelta(days=i)).strftime("%d.%m.") for i, tag_name in enumerate(WOCHENTAGE)}
    except Exception:
        return {tag: "" for tag in WOCHENTAGE}

async def fuehre_bereinigung_durch(channel, raid_id):
    global planung_daten
    guild_id = str(channel.guild.id)
    
    if guild_id not in planung_daten or raid_id not in planung_daten[guild_id]: return

    heute = datetime.now()
    wochen_geloescht = False
    raid_daten = planung_daten[guild_id][raid_id]

    for woche_key in list(raid_daten.keys()):
        # NEU: Selbstreparatur! Falls das kaputte 'overrides' noch existiert, lösche es heimlich.
        if woche_key == "overrides":
            del raid_daten[woche_key]
            speichere_alles()
            continue

        try:
            start_date = datetime.strptime(woche_key, "%d.%m.%Y")
            end_date = start_date + timedelta(days=6)
            if heute.date() > end_date.date():
                del raid_daten[woche_key]
                wochen_geloescht = True
                logger.info(f"🧹 [BEREINIGUNG] Altlast entfernt: Woche {woche_key} (Raid: {raid_id}) gelöscht.")
        except Exception:
            # Fehler stumm schalten, anstatt das Log vollzuspammen
            pass

    if wochen_geloescht:
        speichere_alles()
        await aktualisiere_master_dashboard(channel, raid_id)

@tasks.loop(minutes=10)
async def auto_update_live_timer():
    for guild_id_str, raids in planung_config.items():
        if not isinstance(raids, dict): continue
        
        for raid_id, cfg in raids.items():
            if not isinstance(cfg, dict): continue
                
            live_msg_id = cfg.get("live_termin_msg_id")
            live_channel_id = cfg.get("live_termin_channel_id")
            
            if live_msg_id and live_channel_id:
                try:
                    channel = bot.get_channel(live_channel_id)
                    if channel:
                        msg = await channel.fetch_message(live_msg_id)
                        # Aktualisiert die Nachricht mit den neusten Berechnungen
                        await msg.edit(embed=erstelle_live_termin_embed(guild_id_str, raid_id))
                except discord.NotFound:
                    # Die Nachricht wurde im Channel gelöscht -> ignorieren
                    pass
                except discord.HTTPException:
                    # Discord API ist kurz überlastet -> einfach beim nächsten Loop wieder versuchen
                    pass
                except Exception as e:
                    logger.error(f"⚠️ Unbekannter Fehler im Auto-Updater für {raid_id}: {e}")

@tasks.loop(hours=1)
async def abgelaufene_wochen_checker():
    # Durchsucht nun alle Server und alle dortigen Raids
    for guild_id_str, raids in planung_config.items():
        for raid_id, cfg in raids.items():
            if cfg.get("uebersicht_channel_id"):
                channel = bot.get_channel(cfg["uebersicht_channel_id"])
                if channel: await fuehre_bereinigung_durch(channel, raid_id)

@tasks.loop(minutes=1)
async def raid_reminder_loop():
    jetzt = datetime.now(ZoneInfo("Europe/Berlin"))

    abgelaufen = []
    for msg_id, daten in list(gesendete_pings.items()):
        if jetzt >= daten["loesch_zeit"]:
            
            # 1. Kanal VOR dem try-Block holen, damit er sicher existiert
            channel = bot.get_channel(daten["channel_id"])
            
            # 2. Prüfen, ob der Kanal überhaupt (noch) existiert
            if channel:
                try:
                    msg = await channel.fetch_message(int(msg_id))
                    await msg.delete()
                    # Hier ist dein angepasster Log-Text:
                    logger.info(f"📢 [Reminder] Ping für {msg_id} wurde erfolgreich in #{channel.name} (Server: {channel.guild.name}) gelöscht.")
                except discord.NotFound:
                    logger.info(f"ℹ️ [AUTO-PING INFO] Nachricht (ID: {msg_id}) wurde bereits manuell gelöscht.")
                except Exception as e:
                    logger.error(f"❌ [AUTO-PING FEHLER] Konnte Nachricht (ID: {msg_id}) nicht löschen: {e}")
            else:
                # Falls der Kanal gelöscht wurde, während der Timer noch lief
                logger.error(f"❌ [AUTO-PING FEHLER] Kanal mit ID {daten['channel_id']} wurde nicht gefunden!")

            # WICHTIG: Die Nachricht muss immer aus der Liste entfernt werden, 
            # egal ob erfolgreich gelöscht, manuell gelöscht oder Kanal weg.
            # Sonst versucht der Bot jede Minute wieder, sie zu löschen!
            abgelaufen.append(msg_id)

    # Wenn Pings abgearbeitet wurden, aus der JSON-Datei entfernen
    if abgelaufen:
        for msg_id in abgelaufen:
            del gesendete_pings[msg_id]

    

    # --- SCHRITT 2: NEUE PINGS PRÜFEN ---
    config_wurde_geaendert = False

    for guild_id_str, guild_config in planung_config.items():
        guild = bot.get_guild(int(guild_id_str))
        if not guild: continue
            
        for raid_id, config in guild_config.items():
            if "ping_vorlauf_minuten" not in config: 
                continue
                
            # Hier nutzt der Bot DEINE Funktion aus dem Timer!
            startzeit = hole_naechsten_raid_live(guild_id_str, raid_id) 

            if not startzeit:
                continue

            # Wann soll der Ping rausgehen?
            ping_zeit = startzeit - timedelta(minutes=config["ping_vorlauf_minuten"])
            
            # Anti-Spam: Haben wir DIESEN spezifischen Termin schon gepingt?
            termin_id = startzeit.strftime("%d.%m.%Y_%H:%M")
            if config.get("letzter_ping_gesendet") == termin_id:
                continue
                
            if jetzt >= ping_zeit and jetzt < startzeit:
                channel = guild.get_channel(config["ping_channel_id"])
                
                rollen_name = config.get("raid_rolle_name")
                if not rollen_name: continue
                
                rolle = discord.utils.get(guild.roles, name=rollen_name)
                
                if channel and rolle:
                    minuten_bis_start = int((startzeit - jetzt).total_seconds() / 60)
                    
                    # 1. Den gespeicherten Rohtext abrufen
                    rohtext = config.get("ping_nachricht", "[rolle] ⚔️ Macht euch bereit! Der Raid **[raid]** startet in ca. [minuten] Minuten!")
                    
                    # 2. Die Platzhalter durch die echten Discord-Mentions und Zeiten ersetzen
                    fertiger_text = rohtext.replace("[rolle]", rolle.mention).replace("[raid]", raid_id.upper()).replace("[minuten]", str(minuten_bis_start))
                    
                    try:
                        # 3. Den personalisierten Text senden
                        msg = await channel.send(fertiger_text)
                        
                        # --- LOGGING: Wir loggen, dass der Bot seine Arbeit gemacht hat ---
                        logger.info(f"📢 [Reminder] Ping für '{raid_id}' wurde erfolgreich in #{channel.name} (Server: {guild.name}) gepostet.")
                        
                        # Für das automatische Löschen vormerken
                        gesendete_pings[msg.id] = {
                            "channel_id": channel.id,
                            "loesch_zeit": jetzt + timedelta(minutes=config["ping_loesch_dauer_minuten"])
                        }
                        
                        # Termin als "erledigt" markieren
                        config["letzter_ping_gesendet"] = termin_id
                        config_wurde_geaendert = True
                        
                    except discord.Forbidden:
                        logger.error(f"❌ [Reminder] Bot fehlen die Rechte, um in #{channel.name} (Server: {guild.name}) zu schreiben!")

    # Speichern der Config (nur wenn neue Pings vermerkt wurden)
    if config_wurde_geaendert:
        with open("planung_config.json", "w", encoding="utf-8") as f:
            json.dump(planung_config, f, indent=4)

def erstelle_uebersicht_embed(guild_id_str, raid_id_str):
    LEADER_EMOJI = "<:lead:1512109107958776069>" 
    
    embed_auswertung = discord.Embed(
        title=f"⚔️ Raid Übersicht: {raid_id_str.upper().replace('_', ' ')} ⚔️",
        color=discord.Color.purple()
    )
    # Lade nur die Daten für diesen spezifischen Raid
    server_daten = planung_daten.get(guild_id_str, {}).get(raid_id_str, {})
    
    if not server_daten:
        embed_auswertung.description = f"## Raid-Termine ({raid_id_str})\n\nNoch keine aktiven Wochen geplant."
        return embed_auswertung

    full_text = ""
    try: sortierte_wochen = sorted(server_daten.keys(), key=lambda x: datetime.strptime(x, "%d.%m.%Y"))
    except: sortierte_wochen = sorted(server_daten.keys())

    for woche_key in sortierte_wochen:
        wochen_dates = hole_wochen_daten(woche_key)
        wochen_daten = server_daten[woche_key]
        full_text += f"## 📅 {wochen_dates.get('Montag', '')} - {wochen_dates.get('Sonntag', '')}\n"
        
        termine_text = ""
        for tag in WOCHENTAGE:
            tag_monospace = f"`{tag:<10}`"
           
            if "overrides" in wochen_daten and tag in wochen_daten["overrides"]:
                override_val = wochen_daten["overrides"][tag]
                if override_val == "abgesagt":
                    termine_text += f"🔴 {tag_monospace} ➔ {LEADER_EMOJI} **Raid wurde abgesagt**\n"
                    continue 
                elif isinstance(override_val, dict) and override_val.get("status") == "manuell":
                    start_str, end_str = override_val.get("start_str"), override_val.get("end_str")
                    dauer_str = override_val.get("dauer_str", "")
                    termine_text += f"🟢 {tag_monospace} ➔ {LEADER_EMOJI} **{start_str} - {end_str} Uhr** `[{dauer_str}]`\n"
                    continue

            ist_unmoeglich = False
            for spieler_name, spieler_info in wochen_daten.items():
                if spieler_name == "overrides": continue
                if not isinstance(spieler_info, dict): continue
                tag_daten = spieler_info.get("tage", {}).get(tag, None)
                if tag_daten is None: continue

                if isinstance(tag_daten, str) and "gar nicht" in tag_daten.lower(): ist_unmoeglich = True; break
                elif isinstance(tag_daten, dict) and (tag_daten.get("status") == "gar_nicht" or "gar nicht" in tag_daten.get("text", "").lower()): ist_unmoeglich = True; break

            if ist_unmoeglich:
                termine_text += f"🔴 {tag_monospace} ➔ **Kein Raid** (Spieler fehlt)\n"
                continue

            best_start, best_end, anzahl_eintraege, hat_garnicht = 0, 3000, 0, False
            eingetragene_zeiten = []
            
            for user_name, user_data in wochen_daten.items():
                if user_name == "overrides" or not isinstance(user_data, dict): continue
                tage_dict = user_data.get("tage", {})
                if tag in tage_dict:
                    anzahl_eintraege += 1
                    if tage_dict[tag] == "❌ Gar nicht" or (isinstance(tage_dict[tag], dict) and tage_dict[tag].get("status") == "gar_nicht"): hat_garnicht = True
                    else: eingetragene_zeiten.append(tage_dict[tag])
            
            # WICHTIG: Holt die dynamische Gruppengröße für GENAU DIESEN Raid
            max_spieler = hole_einstellung(guild_id_str, raid_id_str, "gruppen_groesse", 8)
            counter_str = f"`({anzahl_eintraege}/{max_spieler})`"
            
            if anzahl_eintraege < max_spieler: termine_text += f"🟡 {tag_monospace} ➔ {counter_str} *Wartet auf Einträge*\n"
            elif hat_garnicht: termine_text += f"🔴 {tag_monospace} ➔ {counter_str} Kein Raid (Spieler fehlt)\n"
            else:
                for zeit in eingetragene_zeiten:
                    best_start = max(best_start, zeit["von"])
                    best_end = min(best_end, zeit["bis"])
                if best_start < best_end:
                    dauer_minuten = best_end - best_start
                    std, mn = dauer_minuten // 60, dauer_minuten % 60
                    dauer_str = f"Time: {std:02d}:{mn:02d} "                   
                    start_str, end_str = f"{(best_start % 1440) // 60:02d}:{best_start % 60:02d}", f"{(best_end % 1440) // 60:02d}:{best_end % 60:02d}"
                    if best_end >= 1440: end_str += ""
                    termine_text += f"🟢 {tag_monospace} ➔ **{start_str} - {end_str} Uhr** `[{dauer_str}]`\n"
                else:
                    termine_text += f"🔴 {tag_monospace} ➔ **Kein Raid** (Keine Zeit gefunden)\n"
                    
        full_text += termine_text + "\n───────────────────\n\n"

    embed_auswertung.description = full_text
    embed_auswertung.set_image(url="https://cdn.discordapp.com/attachments/1507737909451817041/1520847719361417276/Unbenannt2.png?ex=6a42af50&is=6a415dd0&hm=1e2dce8a00c6a9fb7a10083364826f0e0ece4a393e03ce2cc6dbe0f9156b79d3&")
    return embed_auswertung

def erstelle_user_embed(woche_key, guild_id_str, raid_id_str):
    wochen_dates = hole_wochen_daten(woche_key)
    server_daten = planung_daten.get(guild_id_str, {}).get(raid_id_str, {})
    wochen_daten = server_daten.get(woche_key, {})
    
    embed_user = discord.Embed(
        title=f"📅 Terminplanung: {wochen_dates.get('Montag', '')} - {wochen_dates.get('Sonntag', '')}",
        description="Trage hier deine Zeiten ein.",
        color=discord.Color.blue()
    )
    
    reine_user = {k: v for k, v in wochen_daten.items() if isinstance(v, dict) and "role_pos" in v}
    
    if not reine_user:
        embed_user.add_field(name="Status", value="Noch keine Einträge für diese Woche vorhanden.", inline=False)
    else:
        sorted_users = sorted(reine_user.items(), key=lambda x: x[1].get("role_pos", 0), reverse=True)
        
        for i, (user_name, user_data) in enumerate(sorted_users):
            eintrag_text = ""
            for tag in WOCHENTAGE:
                zeit_info = user_data["tage"].get(tag)
                tag_kurz = tag[:2] 
                
                if not zeit_info: 
                    zeit_str = "⏳"
                    emoji = "⚪"
                elif zeit_info == "❌ Gar nicht" or (isinstance(zeit_info, dict) and zeit_info.get("status") == "gar_nicht"): 
                    zeit_str = "❌"
                    emoji = "🔴"
                else: 
                    zeit_str = (zeit_info['text']
                                .replace('⏱️ ', '')
                                .replace(' Uhr', '')
                                .replace(' (Folgetag)', ' +1')
                                .replace(' (+1)', ' +1'))
                    emoji = "🟢"
                
                eintrag_text += f"{emoji} `{tag_kurz} | {zeit_str:<15}`\n"
                
            embed_user.add_field(
                name=f"{user_data.get('emoji', STANDARD_EMOJI)} {user_name}", 
                value=eintrag_text, 
                inline=True
            )
            
            if (i + 1) % 2 == 0:
                embed_user.add_field(name="\u200b", value="\u200b", inline=True)

    return embed_user
   
    # --- NEU: HILFSFUNKTION FÜR DAS LIVE EMBED ---
async def aktualisiere_master_dashboard(guild_channel, raid_id):
    global planung_config
    guild_id_str = str(guild_channel.guild.id)
    
    logger.info(f"🔄 Versuche Dashboard für '{raid_id}' zu aktualisieren...")
    raid_config = planung_config.get(guild_id_str, {}).get(raid_id, {})
    
    # --- HIER IST DER MAGISCHE AUTO-UPDATER FÜR DAS LIVE-EMBED ---
    async def update_live_embed():
        live_msg_id = raid_config.get("live_termin_msg_id")
        live_channel_id = raid_config.get("live_termin_channel_id")
        if live_msg_id and live_channel_id:
            try:
                live_channel = guild_channel.guild.get_channel(live_channel_id)
                if live_channel:
                    live_msg = await live_channel.fetch_message(live_msg_id)
                    # Überschreibt die alte Nachricht mit den frischen Live-Daten
                    await live_msg.edit(embed=erstelle_live_termin_embed(guild_id_str, raid_id))
                    logger.info("✅ Live-Timer erfolgreich aktualisiert!")
            except Exception as e:
                logger.warning(f"⚠️ Konnte Live-Termin nicht aktualisieren: {e}")
    # -------------------------------------------------------------

    try:
        if raid_config.get("uebersicht_message_id"):
            try:
                msg = await guild_channel.fetch_message(raid_config["uebersicht_message_id"])
                await msg.edit(embed=erstelle_uebersicht_embed(guild_id_str, raid_id))
                logger.info("✅ Dashboard erfolgreich aktualisiert (Edit).")
                
                # WICHTIG: Hier triggern wir den Live-Timer bei jeder Änderung!
                await update_live_embed() 
                return
            except discord.NotFound: 
                logger.warning("⚠️ Alte Dashboard-Nachricht wurde gelöscht. Sende eine neue...")
            except discord.Forbidden:
                logger.error("❌ FEHLER: Der Bot hat keine Berechtigung, die Nachricht zu bearbeiten!")
                return 
            except discord.HTTPException as e:
                logger.warning(f"⏳ Discord API Rate-Limit (zu viele Klicks). Update übersprungen: {e}")
                return 
            except Exception as e:
                logger.error(f"❌ UNBEKANNTER FEHLER beim Editieren: {e}")
                return 

        # Neues Dashboard posten, wenn keins gefunden wurde
        neue_msg = await guild_channel.send(embed=erstelle_uebersicht_embed(guild_id_str, raid_id), view=DashboardView(raid_id))
        planung_config[guild_id_str][raid_id]["uebersicht_message_id"] = neue_msg.id
        planung_config[guild_id_str][raid_id]["uebersicht_channel_id"] = guild_channel.id
        speichere_alles()
        logger.info(f"✅ Neues Dashboard für '{raid_id}' gesendet.")
        
        # WICHTIG: Auch beim komplett neuen Dashboard aktualisieren wir den Timer
        await update_live_embed()
        
    except discord.Forbidden:
        logger.error("🚨 FEHLER: Bot hat keine Erlaubnis in diesem Kanal!")
    except Exception as e:
        logger.error(f"🚨 KRITISCHER FEHLER in aktualisiere_master_dashboard: {e}")

class DashboardView(discord.ui.View):
    # 1. Die __init__ nimmt jetzt die raid_id entgegen
    def __init__(self, raid_id: str): 
        super().__init__(timeout=None)
        self.raid_id = raid_id
        
        # 2. Wir erstellen den Button hier MANUELL, damit wir Variablen nutzen können!
        # Durch f"btn_open_master_plan_{raid_id}" heißt der Button im Hintergrund z.B. "btn_open_master_plan_omega"
        btn = discord.ui.Button(
            label="⌚️ Zeiten eintragen / ändern", 
            style=discord.ButtonStyle.primary, 
            custom_id=f"btn_open_master_plan_{self.raid_id}"
        )
        
        # 3. Wir verbinden den Knopf mit der Funktion weiter unten
        btn.callback = self.open_planning
        
        # 4. Wir fügen den Knopf in die Ansicht ein
        self.add_item(btn)
    
    # ACHTUNG: Hier steht jetzt KEIN @discord.ui.button mehr darüber!
    async def open_planning(self, interaction: discord.Interaction):
        # Wir nutzen jetzt auch hier die erweiterte Funktion für die spezifische raid_id!
        if not hat_raid_rolle(interaction, self.raid_id):
            erlaubte_rolle_name = hole_einstellung(interaction.guild_id, self.raid_id, "raid_rolle_name", "Raidgruppe")
            
            rolle_ping = f"**{erlaubte_rolle_name}**" 
            
            if interaction.guild:
                for role in interaction.guild.roles:
                    if role.name == erlaubte_rolle_name:
                        rolle_ping = role.mention 
                        break
                        
            await interaction.response.send_message(f"<:Warningicon:1518693689633931345> Du hast keine Berechtigung, da dir die Rolle {rolle_ping} fehlt.", ephemeral=True)
            return
            
        # Wir übergeben die raid_id an das nächste Menü (die Wochenauswahl)
        await interaction.response.send_message(
            content=f"☄️ **Willkommen beim Raid-Planner für `{self.raid_id}`!** ☄️\nBitte wähle aus, für welche Woche du dich eintragen möchtest:",
            view=WeekSelectorView(self.raid_id), ephemeral=True
        )
    
class WeekSelectorView(discord.ui.View):
    def __init__(self, raid_id: str):
        super().__init__(timeout=1800.0) 
        self.raid_id = raid_id
        heute = datetime.now()
        montag = heute - timedelta(days=heute.weekday())
        
        for i in range(5):
            woche_start = montag + timedelta(weeks=i)
            woche_ende = woche_start + timedelta(days=6)
            label_str = f"{woche_start.strftime('%d.%m.')} - {woche_ende.strftime('%d.%m.')}"
            woche_key = woche_start.strftime("%d.%m.%Y")
            btn_style = discord.ButtonStyle.primary if i == 0 else discord.ButtonStyle.secondary
            btn = discord.ui.Button(label=label_str, style=btn_style, row=i)
            btn.callback = self.make_week_callback(woche_key)
            self.add_item(btn)

    def make_week_callback(self, woche_key):
        async def callback(interaction: discord.Interaction):
            guild_id_str = str(interaction.guild_id)
            
            # Schachtelung absichern: guild -> raid_id -> woche
            if guild_id_str not in planung_daten: planung_daten[guild_id_str] = {}
            if self.raid_id not in planung_daten[guild_id_str]: planung_daten[guild_id_str][self.raid_id] = {}
            if woche_key not in planung_daten[guild_id_str][self.raid_id]: planung_daten[guild_id_str][self.raid_id][woche_key] = {}
            
            await interaction.response.edit_message(
                content=None, 
                embed=erstelle_user_embed(woche_key, guild_id_str, self.raid_id), 
                view=PlanungsView(woche_key, guild_id_str, interaction.user.id, self.raid_id)
            )
        return callback

    async def zurueck_callback(self, interaction: discord.Interaction):
        await interaction.response.edit_message(content="Bitte wähle eine Raid-Woche aus:", embed=None, view=WeekSelectorView())

class PlanungsView(discord.ui.View):
    def __init__(self, woche_key: str, guild_id_str: str, user_id: int, raid_id: str):
        super().__init__(timeout=1800.0) 
        self.woche_key = woche_key
        self.guild_id_str = guild_id_str
        self.user_id = user_id
        self.raid_id = raid_id
        
        wochen_dates = hole_wochen_daten(woche_key)
        
        for i, tag in enumerate(WOCHENTAGE):
            datum_str = wochen_dates.get(tag, "")
            button_label = f"{tag} ({datum_str})" if datum_str else tag
            btn = discord.ui.Button(label=button_label, row=0 if i < 5 else 1, style=discord.ButtonStyle.secondary)
            btn.callback = self.make_tag_callback(tag)
            self.add_item(btn)
            
        btn_schnell = discord.ui.Button(label="⏭ Schnellwahl (Persönliche Woche)", style=discord.ButtonStyle.blurple, row=2)
        btn_schnell.callback = self.schnell_alle_callback
        self.add_item(btn_schnell)

        zurueck = discord.ui.Button(label="↩️ Zurück", style=discord.ButtonStyle.danger, row=2)
        zurueck.callback = self.zurueck_callback
        self.add_item(zurueck)
        
    def make_tag_callback(self, tag):
        async def callback(interaction: discord.Interaction):
            if not hat_raid_rolle(interaction, self.raid_id): return
            w_dates = hole_wochen_daten(self.woche_key)
            await interaction.response.edit_message(
                content=f"🕘**Wähle deine Zeiten für {tag}:**🕒",
                embed=None, 
                view=TimeSelectionView(tag, self.woche_key, interaction.user.id, self.guild_id_str, self.raid_id)
            )
        return callback

    async def schnell_alle_callback(self, interaction: discord.Interaction):
        # 1. Berechtigungen und Spamschutz
        if not hat_raid_rolle(interaction, self.raid_id): 
            await interaction.response.send_message("<:Warningicon:1518693689633931345> Zugriff verweigert.", ephemeral=True)
            return
            
        un = interaction.user.display_name
        if ist_auf_cooldown(interaction.user.id, interaction.user.display_name):
            await interaction.response.send_message("<:HighWarning:1520330048878411798> **Spamschutz:** Der Bot ist zu langsam für dich, try again. <:HighWarning:1520330048878411798>", ephemeral=True)
            return
            
        # 2. Rolle & Score berechnen
        score, hp, emoji = 0, -1, STANDARD_EMOJI
        if hasattr(interaction.user, "roles"):
            for r in interaction.user.roles:
                if r.name in ROLLEN_PRIORITAET and ROLLEN_PRIORITAET[r.name] > hp:
                    hp = ROLLEN_PRIORITAET[r.name]; score += hp; emoji = ROLLEN_EMOJIS.get(r.name, STANDARD_EMOJI)
        
        # 3. Pfad im Dictionary aufbauen
        if self.guild_id_str not in planung_daten: planung_daten[self.guild_id_str] = {}
        if self.raid_id not in planung_daten[self.guild_id_str]: planung_daten[self.guild_id_str][self.raid_id] = {}
        if self.woche_key not in planung_daten[self.guild_id_str][self.raid_id]: planung_daten[self.guild_id_str][self.raid_id][self.woche_key] = {}
        if un not in planung_daten[self.guild_id_str][self.raid_id][self.woche_key]: 
            planung_daten[self.guild_id_str][self.raid_id][self.woche_key][un] = {"tage": {}, "role_pos": score, "emoji": emoji}

        # --- HIER WAR DER FEHLER: DIE LOGIK FÜR DAS EINTRAGEN DER TAGE FEHLTE! ---
        user_zeiten = user_schnellzeiten.get(self.user_id, {})
        
        # Lade die Fallback-Werte aus den Server-Einstellungen
        def_v_h = hole_einstellung(self.guild_id_str, self.raid_id, "fallback_von_h", 19)
        def_v_m = hole_einstellung(self.guild_id_str, self.raid_id, "fallback_von_m", 0)
        def_b_h = hole_einstellung(self.guild_id_str, self.raid_id, "fallback_bis_h", 22)
        def_b_m = hole_einstellung(self.guild_id_str, self.raid_id, "fallback_bis_m", 0)
        
        for tag in WOCHENTAGE:
            tag_zeit = user_zeiten.get(tag)
            
            # Falls der User für diesen Tag keine spezifische Zeit hat, schaue ob er eine generelle hat, sonst leer
            if not isinstance(tag_zeit, dict):
                tag_zeit = user_zeiten if "von_h" in user_zeiten else {}
            
            von_h = tag_zeit.get("von_h", def_v_h)
            von_m = tag_zeit.get("von_m", def_v_m)
            bis_h = tag_zeit.get("bis_h", def_b_h)
            bis_m = tag_zeit.get("bis_m", def_b_m)
            
            v_min = int(von_h) * 60 + int(von_m)
            b_min = int(bis_h) * 60 + int(bis_m)
            ft = " (+1)" if b_min <= v_min else ""
            if b_min <= v_min: b_min += 1440
            
            # Hier achten wir direkt auf die korrekte :02d Formatierung!
            text_str = f"⏱️ {int(von_h):02d}:{int(von_m):02d} - {int(bis_h):02d}:{int(bis_m):02d} Uhr{ft}"
            
            # Trage den Tag ein
            planung_daten[self.guild_id_str][self.raid_id][self.woche_key][un]["tage"][tag] = {
                "von": v_min, 
                "bis": b_min, 
                "text": text_str
            }

        speichere_alles()
               
        await interaction.response.edit_message(
            content="<:Check:1520327156805275698> **Persönliche Standardzeiten für die komplette Woche eingetragen.**", 
            embed=erstelle_user_embed(self.woche_key, self.guild_id_str, self.raid_id), 
            view=self      
        )

        # WICHTIG: Das Dashboard muss für den spezifischen Raid aktualisiert werden
        await aktualisiere_master_dashboard(interaction.channel, self.raid_id)

    async def zurueck_callback(self, interaction: discord.Interaction):
        await interaction.response.edit_message(content="Bitte wähle eine Raid-Woche aus:", embed=None, view=WeekSelectorView(self.raid_id))

class RaidStatusView(discord.ui.View):
    def __init__(self, guild_id_str, raid_id):
        super().__init__(timeout=1800) 
        self.guild_id_str = guild_id_str
        self.raid_id_str = raid_id
        self.selected_woche = None
        self.selected_tag = None
        server_daten = planung_daten.get(guild_id_str, {}).get(raid_id, {})
        wochen_options = []
        for woche in server_daten.keys():
            if woche == "overrides": continue 
            wochen_options.append(discord.SelectOption(label=f"Woche: {woche}", value=woche, emoji="📅"))
            
        if not wochen_options:
            wochen_options.append(discord.SelectOption(label="Keine Wochen für diesen Raid", value="none"))

        self.woche_select = discord.ui.Select(placeholder="📅 1. Wähle die Woche...", options=wochen_options[:25], row=0)
        self.woche_select.callback = self.woche_callback
        self.add_item(self.woche_select)

        tage_options = [discord.SelectOption(label=tag, value=tag) for tag in WOCHENTAGE]
        self.tag_select = discord.ui.Select(placeholder="📆 2. Wähle den Tag...", options=tage_options, row=1)
        self.tag_select.callback = self.tag_callback
        self.add_item(self.tag_select)

    async def woche_callback(self, interaction: discord.Interaction):
        self.selected_woche = self.woche_select.values[0]
        await interaction.response.defer()

    async def tag_callback(self, interaction: discord.Interaction):
        self.selected_tag = self.tag_select.values[0]
        await interaction.response.defer()

    @discord.ui.button(label="Wieder freigeben", style=discord.ButtonStyle.success, row=2)
    async def freigeben_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.verarbeite_aktion(interaction, "normal")

    @discord.ui.button(label="Raid absagen", style=discord.ButtonStyle.danger, row=2)
    async def absagen_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.verarbeite_aktion(interaction, "abgesagt")

    @discord.ui.button(label="Zeit manuell festlegen", style=discord.ButtonStyle.primary, row=2)
    async def zeit_festlegen_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not self.selected_woche or self.selected_woche == "none":
            await interaction.response.send_message("<:Warningicon:1518693689633931345> Bitte wähle zuerst eine **Woche** aus dem ersten Menü aus!", ephemeral=True)
            return
        if not self.selected_tag:
            await interaction.response.send_message("<:Warningicon:1518693689633931345> Bitte wähle zuerst einen **Tag** aus dem zweiten Menü aus!", ephemeral=True)
            return

        view = LeaderTimeSelectionView(tag=self.selected_tag, woche_key=self.selected_woche, guild_id_str=self.guild_id_str, raid_id=self.raid_id_str)
        await interaction.response.edit_message(
            content=f"<:lead:1512109107958776069> **Raid-Lead Zeitauswahl** für **{self.selected_tag}** (Woche {self.selected_woche}):\nWähle Start- und Endzeit für diesen Tag aus.",
            view=view
        )

    async def verarbeite_aktion(self, interaction: discord.Interaction, aktion: str):
        if not self.selected_woche or self.selected_woche == "none" or not self.selected_tag:
            await interaction.response.send_message("<:Warningicon:1518693689633931345> Bitte wähle zuerst Woche und Tag aus!", ephemeral=True)
            return

        # Korrekte Reihenfolge der Ordner: Guild -> Raid -> Woche
        wochen_daten = planung_daten[self.guild_id_str][self.raid_id_str][self.selected_woche]
        
        if "overrides" not in wochen_daten:
            wochen_daten["overrides"] = {}

        if aktion == "abgesagt":
            # ZURÜCK ZUM TEXT-FORMAT: Das Dashboard erwartet hier exakt den String "abgesagt"
            wochen_daten["overrides"][self.selected_tag] = "abgesagt"
            msg = f"<:dead:1511662916984897566> Der Raid am **{self.selected_tag}** (Woche {self.selected_woche}) wurde erfolgreich abgesagt."
            logger.info(f"👑 [RAID-LEAD] [{interaction.guild.name}] {interaction.user.display_name} hat den Raid am {self.selected_tag} ({self.raid_id_str}) ABGESAGT.")
        else:
            if self.selected_tag in wochen_daten["overrides"]:
                del wochen_daten["overrides"][self.selected_tag]
            msg = f"<:MSQ:1510567044137881711> Der Raid am **{self.selected_tag}** (Woche {self.selected_woche}) ist wieder freigegeben."
            logger.info(f"👑 [RAID-LEAD] [{interaction.guild.name}] {interaction.user.display_name} hat den Raid am {self.selected_tag} ({self.raid_id_str}) FREIGEGEBEN.")
        
        speichere_alles()
        await interaction.response.send_message(f"{msg}\n*(Das Dashboard wird automatisch aktualisiert...)*", ephemeral=True)       
        
        try:
            channel_id = hole_einstellung(self.guild_id_str, self.raid_id_str, "uebersicht_channel_id", None)
            if channel_id:
                dash_channel = interaction.guild.get_channel(channel_id)
                if dash_channel:
                    await aktualisiere_master_dashboard(dash_channel, self.raid_id_str)
                    return
            await aktualisiere_master_dashboard(interaction.channel, self.raid_id_str)
        except Exception as e:
            await interaction.followup.send(f"<:HighWarning:1520330048878411798> Dashboard konnte nicht aktualisiert werden (Fehler: {e}).", ephemeral=True)

class LeaderTimeSelectionView(discord.ui.View):
    def __init__(self, tag: str, woche_key: str, guild_id_str: str, raid_id: str):
        super().__init__(timeout=1800.0)
        self.tag, self.woche_key, self.guild_id_str, self.raid_id = tag, woche_key, guild_id_str, raid_id
        self.von_h, self.bis_h = None, None
        self.von_m, self.bis_m = "00", "00"
        
        h_opts = [discord.SelectOption(label=f"{i:02d} Uhr", value=f"{i:02d}") for i in range(24)]
        m_opts = [discord.SelectOption(label=f":{i:02d}", value=f"{i:02d}") for i in [0, 10, 20, 30, 40, 50]]      
        
        self.v_h = discord.ui.Select(placeholder="VON: Stunde", options=h_opts, row=0); self.v_h.callback = self.vh_cb; self.add_item(self.v_h)
        self.v_m = discord.ui.Select(placeholder="VON: Minute (Optional)", options=m_opts, row=1); self.v_m.callback = self.vm_cb; self.add_item(self.v_m)
        self.b_h = discord.ui.Select(placeholder="BIS: Stunde", options=h_opts, row=2); self.b_h.callback = self.bh_cb; self.add_item(self.b_h)
        self.b_m = discord.ui.Select(placeholder="BIS: Minute (Optional)", options=m_opts, row=3); self.b_m.callback = self.bm_cb; self.add_item(self.b_m)
        
        s_btn = discord.ui.Button(label="💾 Zeit für alle festlegen", style=discord.ButtonStyle.green, row=4); s_btn.callback = self.save_cb; self.add_item(s_btn)
        c_btn = discord.ui.Button(label="❌ Abbrechen", style=discord.ButtonStyle.secondary, row=4); c_btn.callback = self.cancel_cb; self.add_item(c_btn)

    async def vh_cb(self, interaction): self.von_h = self.v_h.values[0]; await interaction.response.defer()
    async def vm_cb(self, interaction): self.von_m = self.v_m.values[0]; await interaction.response.defer()
    async def bh_cb(self, interaction): self.bis_h = self.b_h.values[0]; await interaction.response.defer()
    async def bm_cb(self, interaction): self.bis_m = self.b_m.values[0]; await interaction.response.defer()

    async def cancel_cb(self, interaction):
        view = RaidStatusView(self.guild_id_str, self.raid_id)
        anleitung = (
            "## <a:load:1511667205358489630>  Raid-Status Verwaltung\n"
            "Mit diesem Menü kannst du bestimmte Tage manuell eintragen.\n\n"
            "**Anleitung:**\n"
            "**1️.** Wähle die entsprechende Woche.\n"
            "**2️.** Wähle den betroffenen Wochentag.\n"
            "**3️.** Klicke auf die gewünschte Aktion."
        )
        await interaction.response.edit_message(content=anleitung, view=view)

    async def save_cb(self, interaction):
        if self.von_h is None or self.bis_h is None:
            await interaction.response.send_message("<:HighWarning:1520330048878411798> Bitte wähle zuerst die Start- und End-Stunde aus!", ephemeral=True)
            return

        wochen_daten = planung_daten[self.guild_id_str][self.raid_id][self.woche_key]
        if "overrides" not in wochen_daten:
            wochen_daten["overrides"] = {}

        v_min = int(self.von_h) * 60 + int(self.von_m)
        b_min = int(self.bis_h) * 60 + int(self.bis_m)
        if b_min <= v_min: b_min += 1440

        dauer_minuten = b_min - v_min
        std, mn = dauer_minuten // 60, dauer_minuten % 60
        
        dauer_str = f"Time:{std:02d}:{mn:02d} "
        
        start_str = f"{int(self.von_h):02d}:{int(self.von_m):02d}"
        end_str = f"{int(self.bis_h):02d}:{int(self.bis_m):02d}"

        wochen_daten["overrides"][self.tag] = {
            "status": "manuell",
            "von": v_min,
            "bis": b_min,
            "start_str": start_str,
            "end_str": end_str,
            "dauer_str": dauer_str
        }
        speichere_alles()
        
        logger.info(f"👑 [RAID-LEAD ZEIT] [{interaction.guild.name}] {interaction.user.display_name} hat den {self.tag} ({self.raid_id}) manuell auf {start_str} - {end_str} festgelegt.")

        msg = f"<:lead:1512109107958776069> Der Raid am **{self.tag}** (Woche {self.woche_key}) wurde manuell auf **{start_str} - {end_str} Uhr** festgelegt."
        await interaction.response.send_message(f"{msg}\n*(Das Dashboard wird automatisch aktualisiert...)*", ephemeral=True)       
        
        try:
            channel_id = hole_einstellung(self.guild_id_str, self.raid_id, "uebersicht_channel_id", None)
            if channel_id:
                dash_channel = interaction.guild.get_channel(channel_id)
                if dash_channel: await aktualisiere_master_dashboard(dash_channel, self.raid_id); return
            await aktualisiere_master_dashboard(interaction.channel, self.raid_id)
        except Exception:
            pass
class TimeSelectionView(discord.ui.View):
    def __init__(self, tag, woche_key, user_id, guild_id_str, raid_id):
        super().__init__(timeout=1800.0)
        self.tag = tag
        self.woche_key = woche_key
        self.user_id = user_id
        self.guild_id_str = guild_id_str
        self.raid_id = raid_id
        self.von_h = None
        self.bis_h = None
        self.von_m = 0  # Standard auf 0 Minuten setzen
        self.bis_m = 0
        
        h_opts = [discord.SelectOption(label=f"{i:02d} Uhr", value=f"{i:02d}") for i in range(24)]
        m_opts = [discord.SelectOption(label=f":{i:02d}", value=f"{i:02d}") for i in [0, 10, 20, 30, 40, 50]]      
        self.v_h = discord.ui.Select(placeholder="VON: Stunde", options=h_opts, row=0); self.v_h.callback = self.vh_cb; self.add_item(self.v_h)
        self.v_m = discord.ui.Select(placeholder="VON: Minute (Optional)", options=m_opts, row=1); self.v_m.callback = self.vm_cb; self.add_item(self.v_m)
        self.b_h = discord.ui.Select(placeholder="BIS: Stunde", options=h_opts, row=2); self.b_h.callback = self.bh_cb; self.add_item(self.b_h)
        self.b_m = discord.ui.Select(placeholder="BIS: Minute (Optional)", options=m_opts, row=3); self.b_m.callback = self.bm_cb; self.add_item(self.b_m)
        
        if self.woche_key == "standard_einrichtung":
            s_btn = discord.ui.Button(label="Speichern", style=discord.ButtonStyle.green, row=4); s_btn.callback = self.save_cb; self.add_item(s_btn)
            c_btn = discord.ui.Button(label="↩️ Zurück", style=discord.ButtonStyle.secondary, row=4); c_btn.callback = self.cancel_cb; self.add_item(c_btn)
        else:
            s_btn = discord.ui.Button(label="Zeit Speichern", style=discord.ButtonStyle.green, row=4); s_btn.callback = self.save_cb; self.add_item(s_btn)
            g_btn = discord.ui.Button(label="Gar nicht", style=discord.ButtonStyle.red, row=4); g_btn.callback = self.none_cb; self.add_item(g_btn)
            sz_btn = discord.ui.Button(label="⏭ Schnellwahl", style=discord.ButtonStyle.primary, row=4); sz_btn.callback = self.schnell_cb; self.add_item(sz_btn)
            c_btn = discord.ui.Button(label="Zurück", style=discord.ButtonStyle.secondary, row=4); c_btn.callback = self.cancel_cb; self.add_item(c_btn)

    def ensure_structure(self, interaction):
        g_id = self.guild_id_str
        r_id = self.raid_id
        w_key = self.woche_key
        user = str(interaction.user.display_name)

        if g_id not in planung_daten: planung_daten[g_id] = {}
        if r_id not in planung_daten[g_id]: planung_daten[g_id][r_id] = {}
        if w_key not in planung_daten[g_id][r_id]: planung_daten[g_id][r_id][w_key] = {}
        if user not in planung_daten[g_id][r_id][w_key]:
            planung_daten[g_id][r_id][w_key][user] = {"tage": {}}
            logger.info(f"⚙️ [NEW_USER] [{interaction.guild.name}] {interaction.user.display_name} hat das erste Mal Daten eingetragen.")
        return planung_daten[g_id][r_id][w_key][user]["tage"]   
         
    def bereite_daten(self, interaction):
        un = interaction.user.display_name
        score, hp, emoji = 0, -1, STANDARD_EMOJI
        if hasattr(interaction.user, "roles"):
            for r in interaction.user.roles:
                if r.name in ROLLEN_PRIORITAET and ROLLEN_PRIORITAET[r.name] > hp:
                    hp = ROLLEN_PRIORITAET[r.name]; score += hp; emoji = ROLLEN_EMOJIS.get(r.name, STANDARD_EMOJI)
        
        # HIER WURDE DIE RAID_ID EINGEBAUT:
        if self.guild_id_str not in planung_daten: planung_daten[self.guild_id_str] = {}
        if self.raid_id not in planung_daten[self.guild_id_str]: planung_daten[self.guild_id_str][self.raid_id] = {}
        if self.woche_key not in planung_daten[self.guild_id_str][self.raid_id]: planung_daten[self.guild_id_str][self.raid_id][self.woche_key] = {}
        if un not in planung_daten[self.guild_id_str][self.raid_id][self.woche_key]: 
            planung_daten[self.guild_id_str][self.raid_id][self.woche_key][un] = {"tage": {}, "role_pos": score, "emoji": emoji}
        return un

    async def save_cb(self, interaction: discord.Interaction):
        # 1. Cooldown-Check
        if ist_auf_cooldown(interaction.user.id, interaction.user.display_name):
            await interaction.response.send_message("<a:Cat_Dead:1515301705913467012> **Spamschutz:** Der Bot ist zu langsam für dich, try again. <a:Cat_Dead:1515301705913467012>", ephemeral=True)
            return

        # 2. Input-Check
        if self.von_h is None or self.bis_h is None:
            await interaction.response.send_message("<:HighWarning:1520330048878411798> Bitte wähle zuerst die Start- und End-Stunde aus!", ephemeral=True)
            return

        # 3. Logik-Verzweigung
        if self.woche_key == "standard_einrichtung":
            if self.user_id not in user_schnellzeiten: user_schnellzeiten[self.user_id] = {}
            if "von_h" in user_schnellzeiten[self.user_id]: user_schnellzeiten[self.user_id] = {t: user_schnellzeiten[self.user_id].copy() for t in WOCHENTAGE}
            
            user_schnellzeiten[self.user_id][self.tag] = {
                "von_h": int(self.von_h), "von_m": int(self.von_m),
                "bis_h": int(self.bis_h), "bis_m": int(self.bis_m)
            }
            speichere_alles()
            logger.info(f"⚙️ [STANDARDZEIT][{interaction.guild.name}] {interaction.user.display_name} hat eine neue Standardzeit für {self.tag} gespeichert.")
            try: 
                await interaction.response.edit_message(content=f"<:Check:1520327156805275698> Standardzeit für **{self.tag}** gespeichert!\nDu kannst nun weitere Tage anpassen:", view=StandardzeitTagView(self.user_id))
            except discord.NotFound: pass
            
        else:
            # --- HIER IST DER FIX ---
            # 1. Stelle sicher, dass der User-Eintrag existiert (bereite_daten)
            un = self.bereite_daten(interaction)
            
            # 2. Stelle sicher, dass der Pfad (inkl. raid_id) existiert und hole das Tage-Dict
            tage_dict = self.ensure_structure(interaction)
            
            # 3. Berechnungen
            v_min = int(self.von_h)*60 + int(self.von_m)
            b_min = int(self.bis_h)*60 + int(self.bis_m)
            ft = " (Folgetag)" if b_min <= v_min else ""
            if b_min <= v_min: b_min += 1440
            
            tage_dict[self.tag] = {
                "von": v_min, 
                "bis": b_min, 
                "text": f"⏱️ {int(self.von_h):02d}:{int(self.von_m):02d} - {int(self.bis_h):02d}:{int(self.bis_m):02d} Uhr{ft}"
            }
            
            speichere_alles()
            logger.info(f"🟢 [ZEIT EINGETRAGEN][{interaction.guild.name}] {interaction.user.display_name} hat für '{self.tag}' ({self.raid_id}) eingetragen.")
            await self.finish(interaction)
    
    async def schnell_cb(self, interaction):
        # Ändere zu:
        if ist_auf_cooldown(interaction.user.id, interaction.user.display_name):
            await interaction.response.send_message("<a:Cat_Dead:1515301705913467012> **Spamschutz:** Der Bot ist zu langsam für dich, try again. <a:Cat_Dead:1515301705913467012>", ephemeral=True)
            return
            
        un = self.bereite_daten(interaction)
        sz = user_schnellzeiten.get(self.user_id, {})
        if "von_h" in sz: cz = sz
        else: cz = sz.get(self.tag, {})
        # Lade die Fallback-Werte aus den Server-Einstellungen
        def_v_h = hole_einstellung(self.guild_id_str, self.raid_id, "fallback_von_h", 19)
        def_v_m = hole_einstellung(self.guild_id_str, self.raid_id, "fallback_von_m", 0)
        def_b_h = hole_einstellung(self.guild_id_str, self.raid_id, "fallback_bis_h", 22)
        def_b_m = hole_einstellung(self.guild_id_str, self.raid_id, "fallback_bis_m", 0)

            # Nutze diese Variablen anstelle der festen 19 und 22
        von_h = cz.get("von_h", def_v_h)
        von_m = cz.get("von_m", def_v_m)
        bis_h = cz.get("bis_h", def_b_h)
        bis_m = cz.get("bis_m", def_b_m)

        v_min, b_min = von_h * 60 + von_m, bis_h * 60 + bis_m
        ft = " (+1)" if b_min <= v_min else ""
        if b_min <= v_min: b_min += 1440
        
        planung_daten[self.guild_id_str][self.raid_id][self.woche_key][un]["tage"][self.tag] = {
            "von": v_min,
            "bis": b_min,
            "text": f"⏱️ {int(von_h):02d}:{int(von_m):02d} - {int(bis_h):02d}:{int(bis_m):02d} Uhr{ft}"
        }
        speichere_alles()

        # NEUES LOG:
        logger.info(f"⏭️ [TAGES-SCHNELLWAHL][{interaction.guild.name}] {interaction.user.display_name} hat für '{self.tag}' (Woche {self.woche_key}) die Schnellwahl genutzt.")
        await self.finish(interaction)

    async def none_cb(self, interaction: discord.Interaction):
        # 1. Cooldown-Check
        if ist_auf_cooldown(interaction.user.id, interaction.user.display_name):
            await interaction.response.send_message("<a:Cat_Dead:1515301705913467012> **Spamschutz:** Der Bot ist zu langsam für dich, try again. <a:Cat_Dead:1515301705913467012>", ephemeral=True)
            return
            
        # 2. User-Daten vorbereiten (Initialisierung)
        un = self.bereite_daten(interaction)
        
        # 3. Sicherstellen, dass die Struktur (Guild -> Raid -> Woche) existiert
        # und direkt das passende 'tage'-Dictionary abgreifen
        tage_dict = self.ensure_structure(interaction)
        
        # 4. Den Status setzen
        tage_dict[self.tag] = "❌ Gar nicht"
        
        # 5. Speichern und Logging
        speichere_alles()
        logger.info(f"🔴 [GAR NICHT] [{interaction.guild.name}] {interaction.user.display_name} hat sich für '{self.tag}' (Woche {self.woche_key}) abgemeldet.")
        
        # 6. UI aktualisieren
        await self.finish(interaction)

    async def cancel_cb(self, interaction): 
        if self.woche_key == "standard_einrichtung":
            await interaction.response.edit_message(content="Bitte wähle den Wochentag aus, für den du eine Standardzeit hinterlegen möchtest:", view=StandardzeitTagView(self.user_id))
        else:
            await self.finish(interaction)

    async def finish(self, interaction):
        await interaction.response.edit_message(
            content=None, 
            embed=erstelle_user_embed(self.woche_key, self.guild_id_str, self.raid_id), # raid_id hinzugefügt
            view=PlanungsView(self.woche_key, self.guild_id_str, self.user_id, self.raid_id) # raid_id hinzugefügt
        )
        await aktualisiere_master_dashboard(interaction.channel, self.raid_id) # raid_id hinzugefügt
        
    async def vh_cb(self, interaction): self.von_h = self.v_h.values[0]; await interaction.response.defer()
    async def vm_cb(self, interaction): self.von_m = self.v_m.values[0]; await interaction.response.defer()
    async def bh_cb(self, interaction): self.bis_h = self.b_h.values[0]; await interaction.response.defer()
    async def bm_cb(self, interaction): self.bis_m = self.b_m.values[0]; await interaction.response.defer()

@bot.event
async def on_ready():
    logger.info(f"🟢 Bot erfolgreich eingeloggt als {bot.user.name}")
    
    # Lade alle persistenten Dashboard-Views neu
    for guild_id_str, server_raids in planung_config.items():
        # Versuche, den Servernamen anhand der ID aus dem Bot-Cache zu laden
        guild = bot.get_guild(int(guild_id_str))
        server_name = guild.name if guild else f"Unbekannter Server (ID: {guild_id_str})"
        
        if isinstance(server_raids, dict):
            for raid_id in server_raids.keys():
                bot.add_view(DashboardView(raid_id=raid_id))
                logger.info(f"🔄 [{server_name}] Persistent View reaktiviert für Raid: {raid_id}")
            
    if not abgelaufene_wochen_checker.is_running(): 
        abgelaufene_wochen_checker.start()
        
    # --- NEU HINZUFÜGEN ---
    if not auto_update_live_timer.is_running():
        auto_update_live_timer.start()
        logger.info("⏱️ Hintergrund-Updater für Live-Timer gestartet.")
    # ----------------------
        
    try: 
        await bot.tree.sync()
        logger.info("✅ Slash-Commands erfolgreich synchronisiert.")
    except Exception as e: 
        logger.error(f"❌ Fehler beim Synchronisieren der Commands: {e}")
    try:
        shutil.copy("planung_daten.json", "planung_daten_backup.json")
        shutil.copy("planung_config.json", "planung_config_backup.json")
    except FileNotFoundError:
        pass 
    if not raid_reminder_loop.is_running():
        raid_reminder_loop.start()

@bot.tree.command(name="erstelle-raid", description="Erstellt oder aktualisiert ein spezifisches Raid-Dashboard")
@app_commands.describe(raid_id="Ein eindeutiger Name für diesen Raid")
async def planung(interaction: discord.Interaction, raid_id: str):
    await interaction.response.defer(ephemeral=True)
    guild_id_str = str(interaction.guild_id)
    
    # 1. Namen formatieren (Sicherstellen, dass es keine Leerzeichen gibt)
    raid_id = raid_id.lower().strip().replace(" ", "_")
    
    # 2. Daten-Struktur für diesen Raid vorbereiten
    if guild_id_str not in planung_daten: planung_daten[guild_id_str] = {}
    if raid_id not in planung_daten[guild_id_str]: planung_daten[guild_id_str][raid_id] = {}
    
    # 3. Config-Struktur (Einstellungen) für diesen Raid anlegen, falls neu
    if guild_id_str not in planung_config: planung_config[guild_id_str] = {}
    if raid_id not in planung_config[guild_id_str]:
        # Das sind die Standardwerte für JEDEN neu erstellten Raid
        planung_config[guild_id_str][raid_id] = {
            "uebersicht_channel_id": interaction.channel_id,
            "uebersicht_message_id": None,
            "gruppen_groesse": 8,
            "raid_rolle_name": "Raidgruppe",
            "fallback_von_h": 19, "fallback_von_m": 0,
            "fallback_bis_h": 22, "fallback_bis_m": 0
        }
    else:
        # Falls der Raid schon existiert, aktualisieren wir nur den Kanal
        planung_config[guild_id_str][raid_id]["uebersicht_channel_id"] = interaction.channel_id
        
    speichere_alles()
    
    try:
        # 1. Wir löschen die alte Message-ID, damit der Bot gezwungen ist, neu zu posten
        planung_config[guild_id_str][raid_id]["uebersicht_message_id"] = None
        speichere_alles()
        
        # 2. Jetzt triggern wir die Aktualisierung (die jetzt ein neues Dashboard erstellen MUSS)
        await aktualisiere_master_dashboard(interaction.channel, raid_id)
        
        await interaction.followup.send(f"<:Check:1520327156805275698> Master-Dashboard für den Raid **'{raid_id}'** eingerichtet/aktualisiert!", ephemeral=True)
        logger.info(f"🛠️ [ADMIN] {interaction.user.display_name} hat `/erstelle-raid` für '{raid_id}' ausgeführt.")
        
    except Exception as e:
        await interaction.followup.send(f"<:HighWarning:1520330048878411798> Fehler beim Erstellen des Dashboards: {e}", ephemeral=True)
        logger.error(f"❌ Fehler in /erstelle-raid für '{raid_id}': {e}")

class StandardzeitTagView(discord.ui.View):
    def __init__(self, user_id):
        super().__init__(timeout=1800)
        self.user_id = user_id
        for i, tag in enumerate(WOCHENTAGE):
            btn = discord.ui.Button(label=tag, row=0 if i < 5 else 1, style=discord.ButtonStyle.secondary)
            btn.callback = self.make_tag_callback(tag)
            self.add_item(btn)

    def make_tag_callback(self, tag):
        async def callback(interaction: discord.Interaction):
            view = TimeSelectionView(tag=tag, woche_key="standard_einrichtung", user_id=self.user_id, guild_id_str=str(interaction.guild_id), raid_id="standard_raid") # Wir verwenden eine spezielle raid_id für die Standardzeiten
            await interaction.response.edit_message(
                content=f"🕘 **Wähle deine Standard-Zeit für {tag}:**\n"
                "Bitte wähle deine Standard-Raidzeiten aus. Diese werden bei der ⏭Schnellwahl automatisch eingetragen. ",
                view=view
            )
        return callback

@bot.tree.command(name="standardzeit", description="Lege deine persönliche Standard-Raidzeit für jeden Wochentag fest")
async def standardzeit(interaction: discord.Interaction):
    view = StandardzeitTagView(user_id=interaction.user.id)
    await interaction.response.send_message(
        "**Bitte wähle den Wochentag aus, für den du eine Standardzeit hinterlegen möchtest:**\n"
        "\n\n<:HighWarning:1520330048878411798> Hinweis: Standardzeiten gelten nur für die Schnellwahl und haben keinen Einfluss auf die regulären Einträge..*", 
        view=view, 
        ephemeral=True
    )

@bot.tree.command(name="reset-raid", description="⚠️ Löscht alle eingetragenen Zeiten für einen bestimmten Raid")
@app_commands.describe(raid_id="Die ID des Raids, den du zurücksetzen möchtest")
async def reset_planung(interaction: discord.Interaction, raid_id: str):
    await interaction.response.defer(ephemeral=True)
    guild_id_str = str(interaction.guild_id)
    
    # 1. Namen formatieren (damit er exakt mit der Datenbank übereinstimmt)
    raid_id = raid_id.lower().strip().replace(" ", "_")
    
    # 2. Prüfen, ob der Raid überhaupt existiert
    if guild_id_str in planung_daten and raid_id in planung_daten[guild_id_str]:
        
        # 3. NUR diesen spezifischen Raid leeren
        planung_daten[guild_id_str][raid_id] = {}
        speichere_alles()
        
        # 4. Das richtige Dashboard suchen und aktualisieren
        channel_id = hole_einstellung(guild_id_str, raid_id, "uebersicht_channel_id", None)
        if channel_id:
            dash_channel = interaction.guild.get_channel(channel_id)
            if dash_channel:
                await aktualisiere_master_dashboard(dash_channel, raid_id)
            else:
                await aktualisiere_master_dashboard(interaction.channel, raid_id)
        else:
            await aktualisiere_master_dashboard(interaction.channel, raid_id)
            
        await interaction.followup.send(f"<:Check:1520327156805275698> Alle Raid-Zeiten für den Raid **'{raid_id}'** wurden erfolgreich zurückgesetzt!", ephemeral=True)
        # NEUES LOG:
        logger.info(f"🚨 [RESET] [{interaction.guild.name}] {interaction.user.display_name} hat das Dashboard für '{raid_id}' zurückgesetzt!")
        
    else:
        await interaction.followup.send(f"<:Warningicon:1518693689633931345> Es wurden keine Daten für den Raid **'{raid_id}'** gefunden. Sicher, dass er so heißt?", ephemeral=True)


@bot.tree.command(name="debug_speicher", description="🖥️ (Bot-Owner Befehl)")
async def debug_speicher(interaction: discord.Interaction):
    # 🛑 DER TÜRSTEHER: Prüft, ob du den Befehl ausgeführt hast
    if interaction.user.id != BOT_ENTWICKLER_ID:
        await interaction.response.send_message("<:Warningicon:1518693689633931345> **Zugriff verweigert!** Dieser Befehl ist ein reines Entwickler-Tool und kann nur vom Host-Administrator ausgeführt werden.", ephemeral=True)
        # Wir loggen, falls jemand neugierig war
        logger.warning(f"🛡️ [SICHERHEIT] {interaction.user.display_name} hat versucht, /debug_speicher aufzurufen!")
        return
    guild_id = str(interaction.guild_id)
    
    if guild_id not in planung_daten or not planung_daten[guild_id]:
        await interaction.response.send_message("📭 Die Datenbank für diesen Server ist komplett leer.", ephemeral=True)
        return
        
    daten_string = json.dumps(planung_daten[guild_id], indent=4, ensure_ascii=False)
    file = discord.File(io.BytesIO(daten_string.encode('utf-8')), filename=f"debug_daten_{guild_id}.json")
    
    anleitung = (
        "**Hier ist das Live-Abbild der Datenbank!**\n"
        "Öffne die Datei (z.B. im Browser oder Editor) und suche nach dem betroffenen Tag oder Spieler. "
        "Oft findest du hier kaputte Formatierungen oder Anomalien in den Werten."
    )
    
    await interaction.response.send_message(content=anleitung, file=file, ephemeral=True)
    logger.info(f"🛠️ [DEBUG] [{interaction.guild.name}] {interaction.user.display_name} hat einen Dump der Datenbank über `/debug_speicher` heruntergeladen.")

@bot.tree.command(name="help", description="Zeigt alle wichtigen Informationen und Befehle zum Raid-Planner an")
async def help_command(interaction: discord.Interaction):
    help_text_1 = (
        "## <:bluemark:1511663544717021354> Raid-Planner Hilfe & Infos <:bluemark:1511663544717021354>\n"
        "Hier findest du alle wichtigen Informationen zur Nutzung des Raid-Planners.\n\n"       
        
        "**📝 Wie trage ich mich ein?**\n"
        "Klicke im Master-Dashboard einfach auf den blauen Button **⌚️ Zeiten eintragen / ändern**. "
        "Wähle danach die gewünschte Woche aus und gib deine verfügbaren Zeiten für die jeweiligen Tage an.\n"
        "*Tipp: Nutze die **⏭ Schnellwahl**, um deine hinterlegte Standardzeit mit einem Klick einzutragen!*\n\n"
       
        "**🚦 Das Ampelsystem im Dashboard**\n"
        "🔴 **Rot:** Alle Spieler sind eingetragen, **ABER** es gibt keine gemeinsame Zeit oder jemand hat explizit `❌ Gar nicht` ausgewählt.\n"
        "🟡 **Gelb:** Es haben noch nicht alle Spieler ihre Zeiten eingetragen.\n"
        "🟢 **Grün:** Alle Spieler sind eingetragen **UND** es wurde eine gemeinsame Überschneidung der Zeiten gefunden.\n"
        "<:lead:1512109107958776069> **Raid-Leader:** Der Raid-Leader hat den Tag manuell bearbeitet oder abgesagt.\n\n"        
        
    )
    help_text_2 = ("## **Allgemeine Befehle**\n"
        "`/standardzeit` - Lege deine persönliche Standard-Zeit fest, die du bei der Schnellwahl nutzen möchtest.\n"
        "`/info` - Zeigt dir eine persönliche Übersicht deiner eingetragenen Zeiten und Raids an.\n\n"
        
        "**<:lead:1512109107958776069> Raid-Leader Befehle**\n"
        "`/help` - Zeigt genau diese Hilfeseite (nur für dich sichtbar) an.\n"
        "`/helpa` - Zeigt die Hilfeseite für normale User (Ohne Raid-Leader Befehle) und für alle sichtbar im Kanal.\n"
        "`/timer` - Erstellt ein Embed mit einem Live-Timer, der den Nächsten Raid anzeigt und automatisch aktualisiert wird.\n"
        "`/liste` - Zeigt eine Übersicht aller aktiven Raids auf dem Server an.\n\n"
        "`/erstelle-raid` - Erstellt eine Raid ID oder aktualisiert das Master-Dashboard für einen Raid.\n"
        "`/reset-raid` - Löscht alle eingetragenen Spieler-Zeiten für einen spezifischen Raid.\n"
        "`/status-raid` - Öffnet ein Menü, um Raid-Tage abzusagen oder manuell festzulegen.\n"
        "`/delete-raid` - Löscht einen Raid inklusive Dashboard komplett aus dem System.\n\n"       
                    
        "`/rollen-setup` - Passe die benötigte Rolle für einen Raid an um mit dem Raid zu interagieren.\n"
        "`/uhrzeiten-setup` - Passe die Standardzeiten für Spieler an, die keine Standardzeit hinterlegt haben.\n"
        "`/gruppengröße-setup` - Passe die benötigte Gruppengröße für einen Raid an.\n\n"
        "`/`-Befehle sollten in den Servereinstellungen > Integrationen > Raid Planner konfiguriert werden, damit sie nur für die Raid-Leader sichtbar sind."
    )
    await interaction.response.send_message(help_text_1, ephemeral=True)
    await interaction.followup.send(help_text_2, ephemeral=True)

@bot.tree.command(name="helpa", description="Zeigt alle wichtigen Informationen und Befehle zum Raid-Planner an")
async def helpa_command(interaction: discord.Interaction): 
    help_text = (
        "## <:bluemark:1511663544717021354> Raid-Planner Hilfe & Infos <:bluemark:1511663544717021354>\n"
        "Hier findest du alle wichtigen Informationen zur Nutzung des Raid-Planners.\n\n"       
        
        "**📝 Wie trage ich mich ein?**\n"
        "Klicke im Master-Dashboard einfach auf den blauen Button **⌚️ Zeiten eintragen / ändern**. "
        "Wähle danach die gewünschte Woche aus und gib deine verfügbaren Zeiten für die jeweiligen Tage an.\n"
        "*Tipp: Nutze die **⏭ Schnellwahl**, um deine hinterlegte Standardzeit mit einem Klick einzutragen!*\n\n"
       
        "**🚦 Das Ampelsystem im Dashboard**\n"
        "🔴 **Rot:** Alle Spieler sind eingetragen, **ABER** es gibt keine gemeinsame Zeit oder jemand hat explizit `❌ Gar nicht` ausgewählt.\n"
        "🟡 **Gelb:** Es haben noch nicht alle Spieler ihre Zeiten eingetragen.\n"
        "🟢 **Grün:** Alle Spieler sind eingetragen **UND** es wurde eine gemeinsame Überschneidung der Zeiten gefunden.\n"
        "<:lead:1512109107958776069> **Raid-Leader:** Der Raid-Leader hat den Tag manuell bearbeitet oder abgesagt.\n\n"        
        
        "**Allgemeine Befehle**\n"
        "`/standardzeit` - Lege deine persönliche Standard-Zeit fest, die du bei der Schnellwahl nutzen möchtest.\n"
        "`/info` - Zeigt dir eine persönliche Übersicht deiner eingetragenen Zeiten und Raids an.\n\n"
        
        "*Hinweis: Du benötigst die eingestellte Rolle für den jeweiligen Raid, um dich eintragen zu können.*"  
    )    
    await interaction.response.send_message(help_text, ephemeral=False)

@bot.tree.command(name="status-raid", description="Öffnet ein Menü, um Raid-Tage abzusagen oder wieder freizugeben.")
@app_commands.describe(raid_id="Die ID des Raids")
async def raid_status_command(interaction: discord.Interaction, raid_id: str):
    raid_id = raid_id.lower().strip().replace(" ", "_")
    guild_id_str = str(interaction.guild_id)
    
    if guild_id_str not in planung_daten or raid_id not in planung_daten[guild_id_str] or not planung_daten[guild_id_str][raid_id]:
        await interaction.response.send_message("<:Warningicon:1518693689633931345> Es sind aktuell noch keine Wochen für diesen Raid im System geplant.", ephemeral=True)
        return
    view = RaidStatusView(guild_id_str, raid_id)
    anleitung = (
        "## <a:load:1511667205358489630>  Raid-Status Verwaltung\n"
        "Mit diesem Menü kannst du bestimmte Tage manuell eintragen.\n\n"
        "**Anleitung:**\n"
        "**1️.** Wähle die entsprechende Woche.\n"
        "**2️.** Wähle den betroffenen Wochentag.\n"
        "**3️.** Klicke auf die gewünschte Aktion (Absagen oder Freigeben)."
    )
    await interaction.response.send_message(anleitung, view=view, ephemeral=True)

class GruppenGroesseModal(discord.ui.Modal, title="Gruppengröße anpassen"):
    groesse_input = discord.ui.TextInput(
        label="Anzahl der benötigten Spieler", placeholder="z.B. 8", min_length=1, max_length=2, required=True
    )

    def __init__(self, raid_id: str):
        super().__init__()
        self.raid_id = raid_id

    async def on_submit(self, interaction: discord.Interaction):
        try:
            neue_groesse = int(self.groesse_input.value)
            if neue_groesse <= 0: raise ValueError
            
            guild_id_str = str(interaction.guild_id)
            if guild_id_str not in planung_config: planung_config[guild_id_str] = {}
            if self.raid_id not in planung_config[guild_id_str]: planung_config[guild_id_str][self.raid_id] = {}
            
            # WICHTIG: Speichert jetzt in der Config unter der spezifischen raid_id!
            planung_config[guild_id_str][self.raid_id]["gruppen_groesse"] = neue_groesse
            speichere_alles()
            
            logger.info(f"⚙️ [SETUP] [{interaction.guild.name}] {interaction.user.display_name} hat die Gruppengröße für '{self.raid_id}' auf {neue_groesse} geändert.")
            await interaction.response.send_message(f"<:Check:1520327156805275698> Das Dashboard für **{self.raid_id}** leuchtet ab jetzt grün, sobald **{neue_groesse} Spieler** eingetragen sind!", ephemeral=True)
            
            # Live-Update des passenden Dashboards
            channel_id = planung_config[guild_id_str][self.raid_id].get("uebersicht_channel_id")
            if channel_id:
                channel = interaction.guild.get_channel(channel_id)
                if channel: await aktualisiere_master_dashboard(channel, self.raid_id)
                
        except ValueError:
            await interaction.response.send_message("<:Warningicon:1518693689633931345> Bitte gib eine gültige Zahl ein (größer als 0).", ephemeral=True)

class StandardZeitModal(discord.ui.Modal, title="Standard-Zeiten anpassen"):
    von_input = discord.ui.TextInput(label="Startzeit (Format: HH:MM)", placeholder="19:00", max_length=5, required=True)
    bis_input = discord.ui.TextInput(label="Endzeit (Format: HH:MM)", placeholder="22:00", max_length=5, required=True)

    def __init__(self, raid_id: str):
        super().__init__()
        self.raid_id = raid_id

    async def on_submit(self, interaction: discord.Interaction):
        try:
            v_h, v_m = map(int, self.von_input.value.split(":"))
            b_h, b_m = map(int, self.bis_input.value.split(":"))
            
            guild_id_str = str(interaction.guild_id)
            if guild_id_str not in planung_config: planung_config[guild_id_str] = {}
            if self.raid_id not in planung_config[guild_id_str]: planung_config[guild_id_str][self.raid_id] = {}
            
            planung_config[guild_id_str][self.raid_id]["fallback_von_h"] = v_h
            planung_config[guild_id_str][self.raid_id]["fallback_von_m"] = v_m
            planung_config[guild_id_str][self.raid_id]["fallback_bis_h"] = b_h
            planung_config[guild_id_str][self.raid_id]["fallback_bis_m"] = b_m
            speichere_alles()
            
            logger.info(f"⚙️ [SETUP] [{interaction.guild.name}] {interaction.user.display_name} hat die Fallback-Zeit für '{self.raid_id}' geändert.")
            await interaction.response.send_message(f"<:Check:1520327156805275698> Die Standard-Zeit für **{self.raid_id}** wurde auf **{v_h:02d}:{v_m:02d} - {b_h:02d}:{b_m:02d} Uhr** geändert!", ephemeral=True)
            
        except Exception:
            await interaction.response.send_message("<:Warningicon:1518693689633931345> Ungültiges Format! Bitte nutze zwingend das Format HH:MM (z.B. 19:00).", ephemeral=True)

class RolleModal(discord.ui.Modal, title="Raid-Rolle festlegen"):
    rolle_input = discord.ui.TextInput(label="Name der Discord-Rolle", placeholder="z.B. Raidgruppe", required=True)

    def __init__(self, raid_id: str):
        super().__init__()
        self.raid_id = raid_id

    async def on_submit(self, interaction: discord.Interaction):
        rollen_name = self.rolle_input.value.strip()
        guild_id_str = str(interaction.guild_id)
        
        if guild_id_str not in planung_config: planung_config[guild_id_str] = {}
        if self.raid_id not in planung_config[guild_id_str]: planung_config[guild_id_str][self.raid_id] = {}
        
        planung_config[guild_id_str][self.raid_id]["raid_rolle_name"] = rollen_name
        speichere_alles()
        
        logger.info(f"⚙️ [SETUP] [{interaction.guild.name}] {interaction.user.display_name} hat die Raid-Rolle für '{self.raid_id}' auf '{rollen_name}' geändert.")
        await interaction.response.send_message(f"<:Check:1520327156805275698> Ab sofort darf beim Raid **{self.raid_id}** nur noch die Rolle **{rollen_name}** Zeiten eintragen!", ephemeral=True)


@bot.tree.command(name="gruppengröße-setup", description="⚙️Legt die benötigte Spielerzahl für einen Raid fest")
@app_commands.describe(raid_id="Die ID des Raids")
async def setup_gruppengroesse_command(interaction: discord.Interaction, raid_id: str):
    raid_id = raid_id.lower().strip().replace(" ", "_")
    logger.info(f"⚙️ [SETUP] [{interaction.guild.name}] {interaction.user.display_name} ruft /setup_gruppengröße für '{raid_id}' auf.")
    await interaction.response.send_modal(GruppenGroesseModal(raid_id))

@bot.tree.command(name="uhrzeiten-setup", description="⚙️Ändert die Standard-Zeiten für einen Raid")
@app_commands.describe(raid_id="Die ID des Raids")
async def setup_uhrzeiten_command(interaction: discord.Interaction, raid_id: str):
    raid_id = raid_id.lower().strip().replace(" ", "_")
    logger.info(f"⚙️ [SETUP] [{interaction.guild.name}] {interaction.user.display_name} ruft /uhrzeiten-setup für '{raid_id}' auf.")
    await interaction.response.send_modal(StandardZeitModal(raid_id))

@bot.tree.command(name="rollen-setup", description="⚙️Legt fest, welche Rolle bei einem Raid eintragen darf")
@app_commands.describe(raid_id="Die ID des Raids")
async def setup_rolle_command(interaction: discord.Interaction, raid_id: str):
    raid_id = raid_id.lower().strip().replace(" ", "_")
    logger.info(f"⚙️ [SETUP] [{interaction.guild.name}] {interaction.user.display_name} ruft /rollen-setup für '{raid_id}' auf.")
    await interaction.response.send_modal(RolleModal(raid_id))

@bot.tree.command(name="delete-raid", description="⚠️ Löscht einen Raid komplett aus dem System")
@app_commands.describe(raid_id="Die ID des Raids, der gelöscht werden soll")
async def raid_delete(interaction: discord.Interaction, raid_id: str):
    await interaction.response.defer(ephemeral=True)
    guild_id_str = str(interaction.guild_id)
    raid_id = raid_id.lower().strip().replace(" ", "_")
    
    # 1. Existiert der Raid überhaupt?
    if guild_id_str in planung_daten and raid_id in planung_daten[guild_id_str]:
        
        # 2. Versuch, das Dashboard zu löschen
        try:
            channel_id = hole_einstellung(guild_id_str, raid_id, "uebersicht_channel_id", None)
            msg_id = hole_einstellung(guild_id_str, raid_id, "uebersicht_message_id", None)
            
            if channel_id and msg_id:
                channel = interaction.guild.get_channel(channel_id)
                if channel:
                    msg = await channel.fetch_message(msg_id)
                    await msg.delete()
        except Exception as e:
            logger.warning(f"Konnte Dashboard-Nachricht für {raid_id} nicht löschen (vielleicht schon weg?): {e}")

        # 3. Daten löschen
        del planung_daten[guild_id_str][raid_id]
        
        # Auch die Einstellungen dazu löschen (falls du eine Konfigurations-Struktur hast)
        if guild_id_str in planung_config and raid_id in planung_config[guild_id_str]:
            del planung_config[guild_id_str][raid_id]
            
        speichere_alles()
        
        await interaction.followup.send(f"<:Check:1520327156805275698> Der Raid **'{raid_id}'** wurde inklusive aller Daten und dem Dashboard erfolgreich gelöscht.", ephemeral=True)
        logger.info(f" [DELETE] [{interaction.guild.name}] {interaction.user.display_name} hat den Raid '{raid_id}' komplett gelöscht.")
    else:
        await interaction.followup.send(f"<:Warningicon:1518693689633931345> Raid **'{raid_id}'** konnte nicht gefunden werden.", ephemeral=True)

@bot.tree.command(name="liste", description="Zeigt eine Übersicht aller angelegten Raids an")
async def raid_liste_command(interaction: discord.Interaction):
    guild_id_str = str(interaction.guild_id)
    
    # Hole alle Raids für diesen Server aus der Config und den Daten
    raids_config = planung_config.get(guild_id_str, {})
    raids_daten = planung_daten.get(guild_id_str, {})
    
    # Sammle alle eindeutigen Raid-IDs (falls eine mal nur in einer der beiden Dateien steht)
    alle_raid_ids = set(list(raids_config.keys()) + list(raids_daten.keys()))
    
    if not alle_raid_ids:
        await interaction.response.send_message("📭 Es sind aktuell keine Raids auf diesem Server konfiguriert.", ephemeral=True)
        return

    embed = discord.Embed(
        title="📋 Übersicht deiner aktiven Raids",
        description="Hier ist eine Liste aller Raid-IDs, die aktuell in der Datenbank liegen.\nNutze `/delete-raid <raid_id>`, um alte Gruppen zu entfernen.",
        color=discord.Color.blurple()
    )
    
    for r_id in sorted(alle_raid_ids):
        # 1. Infos aus der Config holen
        config = raids_config.get(r_id, {})
        rolle = config.get("raid_rolle_name", "Raidgruppe (Standard)")
        groesse = config.get("gruppen_groesse", 8)
        channel_id = config.get("uebersicht_channel_id")
        
        channel_mention = f"<#{channel_id}>" if channel_id else "Kein Dashboard-Kanal"
        
        # 2. Infos aus den Daten holen
        daten = raids_daten.get(r_id, {})
        # Wir zählen, wie viele Wochenschlüssel existieren (ohne eventuellen "overrides"-Müll auf dieser Ebene)
        geplante_wochen = len([w for w in daten.keys() if w != "overrides"])
        
        embed.add_field(
            name=f"🆔 `{r_id}`",
            value=f"**Kanal:** {channel_mention}\n**Rolle:** {rolle}\n**Größe:** {groesse} Spieler\n**Aktive Wochen:** {geplante_wochen}",
            inline=False
        )
        
    logger.info(f"📋 [ADMIN] [{interaction.guild.name}] {interaction.user.display_name} hat sich die Raid-Liste anzeigen lassen.")
    await interaction.response.send_message(embed=embed, ephemeral=True)

@bot.tree.command(name="rename", description="Benennt einen bestehenden Raid um.")
@app_commands.describe(alter_name="Der aktuelle Name des Raids", neuer_name="Der neue Name für den Raid")
async def rename(interaction: discord.Interaction, alter_name: str, neuer_name: str):
        guild_id_str = str(interaction.guild_id)
        
        # 1. Prüfen: Existiert der alte Raid überhaupt auf diesem Server?
        config_exists = guild_id_str in planung_config and alter_name in planung_config[guild_id_str]
        daten_exists = guild_id_str in planung_daten and alter_name in planung_daten[guild_id_str]
        
        if not (config_exists or daten_exists):
            await interaction.response.send_message(
                f"<:Warningicon:1518693689633931345> Es wurde auf diesem Server kein Raid mit dem Namen `{alter_name}` gefunden.", 
                ephemeral=True
            )
            return
            
        # 2. Prüfen: Gibt es den neuen Namen schon? (Wir wollen nichts versehentlich überschreiben)
        if guild_id_str in planung_config and neuer_name in planung_config[guild_id_str]:
            await interaction.response.send_message(
                f"<:Warningicon:1518693689633931345> Es existiert bereits ein Raid mit dem Namen `{neuer_name}`. Bitte wähle einen anderen Namen.", 
                ephemeral=True
            )
            return

        # 3. Daten umziehen (Einstellungen)
        if config_exists:
            # .pop(alter_name) holt die Daten raus und löscht gleichzeitig den alten Eintrag
            planung_config[guild_id_str][neuer_name] = planung_config[guild_id_str].pop(alter_name)
            
        # 4. Daten umziehen (Eingetragene Spieler-Zeiten)
        if daten_exists:
            planung_daten[guild_id_str][neuer_name] = planung_daten[guild_id_str].pop(alter_name)
            
        # 5. In den JSON-Dateien speichern
        # HINWEIS: Ersetze 'speichere_alles()' durch den exakten Namen deiner Speicher-Funktion, falls sie anders heißt!
        speichere_alles() 

        rename_text = (  f"<:Check:1520327156805275698> Der Raid wurde erfolgreich von **{alter_name}** zu **{neuer_name}** umbenannt!\n\n"
            f"**<:HighWarning:1520330048878411798> Wichtig für das Dashboard:**\n"
            f"Das alte Dashboard funktioniert nun nicht mehr, da es noch auf `{alter_name}` hört. "
            f"Bitte lösche die alte Dashboard-Nachricht und tippe `/erstelle-raid {neuer_name}` in euren Kanal, "
            f"um das Dashboard mit dem neuen Namen neu zu spawnen. Alle alten Daten sind dort sofort wieder da!")
        
        # 6. Erfolgsmeldung senden
        await interaction.response.send_message(rename_text, ephemeral=True)

def hole_naechsten_raid_live(guild_id_str, raid_id_str):
    server_daten = planung_daten.get(guild_id_str, {}).get(raid_id_str, {})
    if not server_daten:
        return None

    jetzt = datetime.now(ZoneInfo("Europe/Berlin"))
    
    # Sortiere Wochen
    try: sortierte_wochen = sorted(server_daten.keys(), key=lambda x: datetime.strptime(x, "%d.%m.%Y"))
    except: sortierte_wochen = sorted(server_daten.keys())

    for woche_key in sortierte_wochen:
        wochen_daten = server_daten[woche_key]
        try:
            montag_datum = datetime.strptime(woche_key, "%d.%m.%Y").replace(tzinfo=ZoneInfo("Europe/Berlin"))
        except: continue

        for tag_idx, tag in enumerate(WOCHENTAGE):
            tag_datum = montag_datum + timedelta(days=tag_idx)
            
            # --- STRICKTER CHECK: IST DER TAG GRÜN? ---
            # Wir berechnen den Status so, wie das Dashboard es tut
            is_valid = False
            best_start, best_end = 0, 3000
            
            # 1. Check Overrides (Abgesagt oder Manuell?)
            if "overrides" in wochen_daten and tag in wochen_daten["overrides"]:
                ov = wochen_daten["overrides"][tag]
                if ov == "abgesagt": continue # Rot
                if isinstance(ov, dict) and ov.get("status") == "manuell":
                    is_valid = True # Manuell gesetzt = Grün
                    # Bestimme Zeit aus dem Override
                    try:
                        h, m = map(int, ov.get("start_str", "00:00").split(":"))
                        best_start = h * 60 + m
                    except: pass
            
            # 2. Check Automatische Planung
            if not is_valid:
                anzahl_eintraege, hat_garnicht = 0, False
                eingetragene_zeiten = []
                
                # Sammle Spielerdaten
                for user_name, user_data in wochen_daten.items():
                    if user_name == "overrides" or not isinstance(user_data, dict): continue
                    tage_dict = user_data.get("tage", {})
                    if tag in tage_dict:
                        anzahl_eintraege += 1
                        if tage_dict[tag] == "❌ Gar nicht" or (isinstance(tage_dict[tag], dict) and tage_dict[tag].get("status") == "gar_nicht"):
                            hat_garnicht = True
                        else:
                            eingetragene_zeiten.append(tage_dict[tag])
                
                max_spieler = hole_einstellung(guild_id_str, raid_id_str, "gruppen_groesse", 8)
                
                # NUR GRÜN, WENN: Voll, niemand hat 'gar nicht' und es gibt valide Zeiten
                if anzahl_eintraege >= max_spieler and not hat_garnicht and eingetragene_zeiten:
                    for zeit in eingetragene_zeiten:
                        best_start = max(best_start, zeit["von"])
                        best_end = min(best_end, zeit["bis"])
                    
                    if best_start < best_end:
                        is_valid = True
            
            # --- ZUKUNFTS-CHECK ---
            if is_valid:
                stunde = (best_start % 1440) // 60
                minute = best_start % 60
                termin_dt = tag_datum.replace(hour=stunde, minute=minute, second=0, microsecond=0)
                
                if termin_dt > jetzt:
                    return termin_dt
                    
    return None

@bot.tree.command(name="timer", description="Spawnt das Live-Termin-Fenster, das sich ab jetzt selbst aktualisiert.")
@app_commands.describe(raid_id="Die ID des Raids")
async def timer(interaction: discord.Interaction, raid_id: str):
    raid_id = raid_id.lower().strip().replace(" ", "_")
    guild_id_str = str(interaction.guild_id)
    
    if guild_id_str not in planung_config: planung_config[guild_id_str] = {}
    if raid_id not in planung_config[guild_id_str]: planung_config[guild_id_str][raid_id] = {}
    
    # Initiales Embed generieren
    embed = erstelle_live_termin_embed(guild_id_str, raid_id)

    msg = await interaction.channel.send(embed=embed)
    await interaction.response.send_message("<:Check:1520327156805275698> Live-Timer wurde im Kanal erstellt.", ephemeral=True, delete_after=3.0)
    
    
    # Speichern der Message-ID für den Auto-Updater
    planung_config[guild_id_str][raid_id]["live_termin_msg_id"] = msg.id
    planung_config[guild_id_str][raid_id]["live_termin_channel_id"] = interaction.channel_id
    speichere_alles()

def erstelle_live_termin_embed(guild_id_str, raid_id_str):
    termin_dt = hole_naechsten_raid_live(guild_id_str, raid_id_str)
    jetzt_str = datetime.now(ZoneInfo("Europe/Berlin")).strftime('%d.%m.%Y %H:%M:%S')

    if not termin_dt:
        embed = discord.Embed(
            title=f"Timer: {raid_id_str.upper().replace('_', ' ')}",
            description="❌ Aktuell steht kein kommender Raid-Termin fest.\n*(Wartet auf Einträge oder alle Termine sind vorbei)*",
            color=discord.Color.red()
        )
        embed.set_footer(text=f"Letztes Update: {jetzt_str}")
        return embed

    unix_ts = int(termin_dt.timestamp())
    embed = discord.Embed(
        title=f"Timer: {raid_id_str.upper().replace('_', ' ')}",
        color=discord.Color(0x242BBD)
    )
    embed.set_thumbnail(url="https://cdn.discordapp.com/attachments/1507737909451817041/1520723697768468520/26451-timersand.gif?ex=6a423bcf&is=6a40ea4f&hm=3c93bd15c6910df10dcf7ab467eda1390d7c3c4b25eaeddf9b6c819bd7751cef&")
    embed.add_field(name="**Start**", value=f" <t:{unix_ts}:R>", inline=False)
    embed.add_field(name="**Datum & Uhrzeit**", value=f" <t:{unix_ts}:F>", inline=False)   
    embed.set_footer(text=f"Letztes Update: {jetzt_str}")
    
    return embed

gesendete_pings = {}

@bot.tree.command(name="reminder", description="Richtet einen automatischen Ping für einen Raid ein.")
@app_commands.describe(
    raid_id="Für welchen Raid ist dieser Ping?",
    vorlauf_minuten="Wie viele Minuten VOR Raid-Start soll gepingt werden?",
    loesch_minuten="Wie viele Minuten NACH dem Ping soll die Nachricht gelöscht werden?",
    kanal="In welchem Kanal soll die Nachricht gepostet werden?",
    text="Optional: Eigener Text. Bsp: '[rolle] ⚔️ Macht euch bereit! Der Raid **[raid]** startet in ca. [minuten] Minuten!'"
)
async def cmd_auto_ping_setup(interaction: discord.Interaction, raid_id: str, vorlauf_minuten: int, loesch_minuten: int, kanal: discord.TextChannel, text: str = None):
    guild_id_str = str(interaction.guild_id)
    
    if guild_id_str not in planung_config or raid_id not in planung_config[guild_id_str]:
        await interaction.response.send_message("❌ Dieser Raid existiert in der Konfiguration noch nicht.", ephemeral=True)
        return
    
    if text is None:
        text = "[rolle] ⚔️ Macht euch bereit! Der Raid **[raid]** startet in ca. [minuten] Minuten!"
        
    # Neue Werte eintragen
    planung_config[guild_id_str][raid_id]["ping_vorlauf_minuten"] = vorlauf_minuten
    planung_config[guild_id_str][raid_id]["ping_loesch_dauer_minuten"] = loesch_minuten
    planung_config[guild_id_str][raid_id]["ping_channel_id"] = kanal.id
    planung_config[guild_id_str][raid_id]["ping_nachricht"] = text
    
    with open("planung_config.json", "w", encoding="utf-8") as f:
        json.dump(planung_config, f, indent=4)
        
    rollen_name = planung_config[guild_id_str][raid_id].get("raid_rolle_name", "Unbekannt")

    logger.info(f"🛠️ {interaction.user.display_name} hat den Ping für '{raid_id}' auf Server '{interaction.guild.name}' eingerichtet.")
        
    await interaction.response.send_message(f"<:Check:1520327156805275698> Reminder eingerichtet! \nDer Bot wird künftig **{vorlauf_minuten} Minuten** vor Start des Raids **{raid_id.upper()}** die Rolle `@{rollen_name}` im Kanal {kanal.mention} pingen.", ephemeral=True)

@bot.tree.command(name="info", description="Zeigt dir eine persönliche Übersicht deiner eingetragenen Raid-Zeiten.")
async def cmd_user_info(interaction: discord.Interaction):
    guild_id_str = str(interaction.guild_id)
    user_name = interaction.user.display_name
    
    server_daten = planung_daten.get(guild_id_str, {})
    
    if not server_daten:
        await interaction.response.send_message("Auf diesem Server gibt es aktuell keine aktiven Raids.", ephemeral=True)
        return
        
    embed = discord.Embed(
        title=f"📋 Deine Raid-Übersicht, {user_name}",
        description="Hier siehst du deine eingetragenen Raid-Zeiten:",
        color=discord.Color.blue()
    )
    
    # Thumbnail mit dem Profilbild des Users (macht es etwas persönlicher)
    if interaction.user.display_avatar:
        embed.set_thumbnail(url=interaction.user.display_avatar.url)
    
    eintraege_gefunden = False
    jetzt = datetime.now(ZoneInfo("Europe/Berlin"))
    
    # Gehe alle Raids auf dem Server durch
    for raid_id, raid_wochen in server_daten.items():
        raid_text = ""
        
        # Sortiere die Wochen chronologisch
        try: 
            sortierte_wochen = sorted([w for w in raid_wochen.keys() if w != "overrides"], key=lambda x: datetime.strptime(x, "%d.%m.%Y"))
        except: 
            sortierte_wochen = sorted([w for w in raid_wochen.keys() if w != "overrides"])
            
        for woche_key in sortierte_wochen:
            # Überspringe Wochen, die bereits komplett in der Vergangenheit liegen
            try:
                montag = datetime.strptime(woche_key, "%d.%m.%Y").replace(tzinfo=ZoneInfo("Europe/Berlin"))
                sonntag = montag + timedelta(days=6, hours=23, minutes=59)
                if sonntag < jetzt:
                    continue 
            except:
                pass
            
            woche_daten = raid_wochen[woche_key]
            
            # Prüfen, ob der User in dieser Woche existiert
            if user_name in woche_daten:
                user_tage = woche_daten[user_name].get("tage", {})
                
                tage_text = ""
                for tag in WOCHENTAGE:
                    if tag in user_tage:
                        zeit_info = user_tage[tag]
                        
                        # Wir ignorieren "❌ Gar nicht" - man will ja nur sehen, WANN man raidet
                        if zeit_info != "❌ Gar nicht" and not (isinstance(zeit_info, dict) and zeit_info.get("status") == "gar_nicht"):
                            # Hole den formatierten Text (z.B. "⏱️ 19:00 - 22:00 Uhr")
                            text = zeit_info.get("text", "") if isinstance(zeit_info, dict) else zeit_info
                            tage_text += f"- **{tag}:** {text}\n"
                
                # Wenn an mindestens einem Tag in dieser Woche Zeiten stehen, hängen wir sie an
                if tage_text:
                    raid_text += f"\n**Woche ab dem {woche_key}**\n{tage_text}"
        
        # Wenn wir für diesen Raid Einträge gefunden haben, erstellen wir ein Feld im Embed
        if raid_text:
            eintraege_gefunden = True
            embed.add_field(name=f"⚔️ {raid_id.upper().replace('_', ' ')}", value=raid_text, inline=False)
            
    # Falls der User absolut nirgends eingetragen ist
    if not eintraege_gefunden:
        embed.description = "Du hast dich aktuell für keine zukünftigen Termine eingetragen (oder alle deine Termine liegen in der Vergangenheit)."
        
    # ephemeral=True ist hier wichtig, damit niemand anderes den privaten Terminplan des Users sieht
    await interaction.response.send_message(embed=embed, ephemeral=True)

    logger.info(f"🛠️ [{interaction.guild.name}] {interaction.user.display_name} hat /info genutzt")

GF = 341220324333060099

@bot.tree.command(name="termine", description="Zeigt dir die nächsten Termine eines Raids von einem anderen Server.")
@app_commands.describe(
    server_id="Wähle den Server aus",
    raid_id="Wähle den Raid aus"
)
async def termin_command(interaction: discord.Interaction, server_id: str, raid_id: str):
    # 1. Sicherheits-Check ganz am Anfang
    if interaction.user.id != GF and interaction.user.id != BOT_ENTWICKLER_ID:
        await interaction.response.send_message("<:Warningicon:1518693689633931345> **Zugriff verweigert!** Dieser Befehl ist nur für ausgewählte Benutzer zugänglich.", ephemeral=True)
        # Wir loggen, falls jemand neugierig war
        logger.warning(f"🛡️ [SICHERHEIT] {interaction.user.display_name} hat versucht, /termine aufzurufen!")
        return

    # 2. Check, ob die Daten wirklich existieren
    if server_id not in planung_daten or raid_id not in planung_daten.get(server_id, {}):
        await interaction.response.send_message("❌ Dieser Server oder Raid wurde in der Datenbank nicht gefunden.", ephemeral=True)
        return

    # 3. Server-Namen für eine schöne Anzeige holen
    guild = bot.get_guild(int(server_id))
    server_name = guild.name if guild else "Unbekannter Server"
    
    # 4. Das Embed abrufen
    embed = erstelle_live_termin_embed(server_id, raid_id)
    
    # 5. Den Titel des Embeds anpassen, damit der Servername sichtbar ist
    original_title = embed.title if embed.title else "Nächster Raid"
    embed.title = f"🌍 {server_name} | {original_title}"
    
    # 6. Senden (als Embed, nicht als Text-Inhalt!)
    await interaction.response.send_message(embed=embed, ephemeral=True)


# --- HIER KOMMT DIE MAGIE: DIE AUTOVERVOLLSTÄNDIGUNG ---

@termin_command.autocomplete('server_id')
async def server_autocomplete(interaction: discord.Interaction, current: str):
    choices = []
    
    # Gehe alle Server in der Datenbank durch
    for guild_id_str in planung_daten.keys():
        guild = bot.get_guild(int(guild_id_str))
        
        if guild:
            # Prüfen, ob das, was der User tippt, im Server-Namen vorkommt
            if current.lower() in guild.name.lower():
                choices.append(app_commands.Choice(name=guild.name, value=guild_id_str))
                    
    return choices[:25]


@termin_command.autocomplete('raid_id')
async def raid_autocomplete(interaction: discord.Interaction, current: str):
    choices = []
    
    ausgewaehlter_server = interaction.namespace.server_id
    
    if ausgewaehlter_server and ausgewaehlter_server in planung_daten:
        for r_id in planung_daten[ausgewaehlter_server].keys():
            if current.lower() in r_id.lower():
                anzeige_name = r_id.upper().replace('_', ' ')
                choices.append(app_commands.Choice(name=anzeige_name, value=r_id))
                
    return choices[:25]

@bot.tree.command(name="auto-ping-test", description="Sendet sofort einen Test-Ping für einen Raid, um Text und Rechte zu prüfen.")
@app_commands.describe(
    raid_id="Welcher Raid soll getestet werden?",
    kanal="Optional: In welchem Kanal soll der Test-Ping gesendet werden?",
    loesch_sekunden="Nach wie vielen Sekunden soll die Testnachricht gelöscht werden? (Standard: 15 Sek)"
)
async def cmd_auto_ping_test(
    interaction: discord.Interaction, 
    raid_id: str, 
    kanal: discord.TextChannel = None, 
    loesch_sekunden: int = 15
):
    if interaction.user.id != BOT_ENTWICKLER_ID:
            await interaction.response.send_message("<:Warningicon:1518693689633931345> **Zugriff verweigert!** Dieser Befehl ist ein reines Entwickler-Tool und kann nur vom Host-Administrator ausgeführt werden.", ephemeral=True)
            # Wir loggen, falls jemand neugierig war
            logger.warning(f"🛡️ [SICHERHEIT] {interaction.user.display_name} hat versucht, /sysinfo aufzurufen!")
            return
    
    guild_id_str = str(interaction.guild_id)
    
    if guild_id_str not in planung_config or raid_id not in planung_config[guild_id_str]:
        await interaction.response.send_message("❌ Dieser Raid existiert in der Konfiguration noch nicht.", ephemeral=True)
        return
        
    config = planung_config[guild_id_str][raid_id]
    
    # 1. Ziel-Kanal bestimmen (Entweder angegeben, aus Config oder der aktuelle Kanal)
    target_channel = kanal or interaction.guild.get_channel(config.get("ping_channel_id")) or interaction.channel
    
    # 2. Rolle & Text vorbereiten
    rollen_name = config.get("raid_rolle_name")
    rolle = discord.utils.get(interaction.guild.roles, name=rollen_name) if rollen_name else None
    rolle_mention = rolle.mention if rolle else "@TestRolle"
    
    rohtext = config.get("ping_nachricht", "[rolle] ⚔️ Macht euch bereit! Der Raid **[raid]** startet in ca. [minuten] Minuten!")
    
    # Test-Ersetzung mit einem festen Beispielwert (z.B. 15 Minuten)
    fertiger_text = rohtext.replace("[rolle]", rolle_mention).replace("[raid]", raid_id.upper()).replace("[minuten]", "15")
    
    try:
        # 3. Test-Ping absenden
        msg = await target_channel.send(f"🧪 **[TEST-PING]**\n{fertiger_text}")
        
        # LOGGING
        logger.info(f"[TEST-PING] {interaction.user.display_name} hat einen Test-Ping für '{raid_id}' in #{target_channel.name} ausgelöst.")
        
        # 4. Für das automatische Löschen eintragen (in Sekunden für schnelles Testen)
        gesendete_pings[msg.id] = {
            "channel_id": target_channel.id,
            "loesch_zeit": datetime.now().astimezone() + timedelta(seconds=loesch_sekunden)
        }
        
        await interaction.response.send_message(
            f"✅ Test-Ping wurde in {target_channel.mention} gesendet!\n"
            f"Er wird in **{loesch_sekunden} Sekunden** automatisch gelöscht, um das Löschen & Logging zu testen.", 
            ephemeral=True
        )
        
    except discord.Forbidden:
        await interaction.response.send_message(f"❌ Mir fehlen die Rechte, um in {target_channel.mention} Nachrichten zu schreiben!", ephemeral=True)
        logger.error(f"❌ [TEST-PING FEHLER] Keine Schreibrechte in #{target_channel.name}")

# Trage hier DEINE kopierte ID als Zahl ein (ohne Anführungszeichen!)
BOT_ENTWICKLER_ID = 284329623175692290

@bot.tree.command(name="sysinfo", description="🖥️ (Bot-Owner Befehl) ")
async def sysinfo_command(interaction: discord.Interaction):
    if interaction.user.id != BOT_ENTWICKLER_ID:
        await interaction.response.send_message("<:Warningicon:1518693689633931345> **Zugriff verweigert!** Dieser Befehl ist ein reines Entwickler-Tool und kann nur vom Host-Administrator ausgeführt werden.", ephemeral=True)
        # Wir loggen, falls jemand neugierig war
        logger.warning(f"🛡️ [SICHERHEIT] {interaction.user.display_name} hat versucht, /sysinfo aufzurufen!")
        return

    await interaction.response.defer(ephemeral=True)

    # 1. Systemdaten sammeln
    os_name = f"{platform.system()} {platform.release()}"
    
    # CPU
    cpu_auslastung = psutil.cpu_percent(interval=1.0)
    cpu_kerne = psutil.cpu_count(logical=True)
    
    # RAM
    ram = psutil.virtual_memory()
    ram_total = round(ram.total / (1024**3), 2)
    ram_used = round(ram.used / (1024**3), 2)
    ram_percent = ram.percent
    
    # Festplatte (Root-Verzeichnis)
    disk = psutil.disk_usage('/')
    disk_total = round(disk.total / (1024**3), 2)
    disk_used = round(disk.used / (1024**3), 2)
    disk_percent = disk.percent

    # Ping zur Discord-API
    ping = round(bot.latency * 1000)

    # Bot-Uptime berechnen
    laufzeit_sekunden = int(time.time() - BOT_START_ZEIT)
    tage = laufzeit_sekunden // 86400
    stunden = (laufzeit_sekunden % 86400) // 3600
    minuten = (laufzeit_sekunden % 3600) // 60
    uptime_str = f"{tage}T {stunden}S {minuten}M" if tage > 0 else f"{stunden}S {minuten}M"

    # 2. Embed zusammenbauen
    embed = discord.Embed(
        title="🖥️ Server Hardware Monitoring",
        color=discord.Color.dark_theme()
    )
    
    embed.add_field(name="Betriebssystem", value=f"`{os_name}`", inline=False)
    
    embed.add_field(
        name="🧠 CPU", 
        value=f"Auslastung: **{cpu_auslastung}%**\nKerne: **{cpu_kerne}**", 
        inline=True
    )
    embed.add_field(
        name="📊 Arbeitsspeicher", 
        value=f"Auslastung: **{ram_percent}%**\nVerwendet: **{ram_used} GB / {ram_total} GB**", 
        inline=True
    )
    embed.add_field(name="\u200b", value="\u200b", inline=True) # Leeres Feld für schönere Formatierung
    
    embed.add_field(
        name="💾 Festplatte", 
        value=f"Belegt: **{disk_percent}%**\nVerwendet: **{disk_used} GB / {disk_total} GB**", 
        inline=True
    )
    embed.add_field(
        name="🌐 Netzwerk & Bot", 
        value=f"Discord Ping: **{ping} ms**\nBot-Uptime: **{uptime_str}**", 
        inline=True
    )
    
    embed.set_footer(text=f"Angefragt von {interaction.user.display_name}", icon_url=interaction.user.display_avatar.url if interaction.user.display_avatar else None)
    
    # Neues Log eintragen
    logger.info(f"🛠️ [ADMIN] [{interaction.guild.name}] {interaction.user.display_name} hat sich die Server-Hardware (/sysinfo) anzeigen lassen.")
    
    await interaction.followup.send(embed=embed, ephemeral=True)

lade_alles()

LOG_WEBHOOK_URL = "https://discord.com/api/webhooks/1515118277540708432/pX8bnpAzd3YcFxe-k3skM-tXLU1uuTbdWkiHEE3HGYwgv6gF-9LZlanQBbILK2ZFYYsZ"
    
BOT_TOKEN = ""
bot.run(BOT_TOKEN)