import sqlite3
import os
import random
from datetime import date, timedelta
from flask import Flask, render_template, request, redirect, url_for, flash, session
from werkzeug.security import generate_password_hash, check_password_hash

app = Flask(__name__)
app.secret_key = 'your_secret_key'  # CHANGE THIS!

# Database file path
DB_FILE = 'database.db'

def get_db_connection():
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    return conn

def create_tables():
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY,
            username TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL,
            is_admin INTEGER DEFAULT 0
        );
    ''')
    if not cursor.execute('SELECT * FROM users WHERE is_admin = 1').fetchone():
        admin_password_hash = generate_password_hash('rajan123')
        cursor.execute('INSERT INTO users (username, password_hash, is_admin) VALUES (?, ?, ?)',
                       ('admin', admin_password_hash, 1))

    cursor.execute('''
        CREATE TABLE IF NOT EXISTS events (
            id INTEGER PRIMARY KEY,
            name TEXT NOT NULL,
            start_date TEXT NOT NULL
        );
    ''')
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS teams (
            id INTEGER PRIMARY KEY,
            team_name TEXT NOT NULL,
            user_id INTEGER NOT NULL,
            event_id INTEGER NOT NULL,
            FOREIGN KEY (user_id) REFERENCES users(id),
            FOREIGN KEY (event_id) REFERENCES events(id)
        );
    ''')
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS players (
            id INTEGER PRIMARY KEY,
            player_name TEXT NOT NULL,
            team_id INTEGER NOT NULL,
            FOREIGN KEY (team_id) REFERENCES teams(id)
        );
    ''')
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS venues (
            id INTEGER PRIMARY KEY,
            name TEXT UNIQUE NOT NULL,
            is_available INTEGER DEFAULT 1
        );
    ''')
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS matches (
            id INTEGER PRIMARY KEY,
            event_id INTEGER NOT NULL,
            team1_id INTEGER NOT NULL,
            team2_id INTEGER NOT NULL,
            time_slot TEXT NOT NULL,
            match_date TEXT NOT NULL,
            venue_id INTEGER NOT NULL,
            FOREIGN KEY (event_id) REFERENCES events(id),
            FOREIGN KEY (team1_id) REFERENCES teams(id),
            FOREIGN KEY (team2_id) REFERENCES teams(id),
            FOREIGN KEY (venue_id) REFERENCES venues(id)
        );
    ''')
    conn.commit()
    conn.close()

if not os.path.exists(DB_FILE):
    create_tables()

def minutes_to_12h_format(minutes):
    """Convert minutes since midnight to 12-hour format (e.g., 9:00 AM)"""
    hours = minutes // 60
    mins = minutes % 60
    
    if hours == 0:
        period = "AM"
        display_hours = 12
    elif hours < 12:
        period = "AM"
        display_hours = hours
    elif hours == 12:
        period = "PM"
        display_hours = 12
    else:
        period = "PM"
        display_hours = hours - 12
    
    return f"{display_hours}:{mins:02d} {period}"

def is_venue_available(venue_id, match_date, time_slot, conn, exclude_match_id=None):
    """Check if a venue is available at the given date and time"""
    query = '''
        SELECT id FROM matches 
        WHERE venue_id = ? AND match_date = ? AND time_slot = ?
    '''
    params = [venue_id, match_date, time_slot]
    
    if exclude_match_id:
        query += ' AND id != ?'
        params.append(exclude_match_id)
    
    existing_match = conn.execute(query, params).fetchone()
    return existing_match is None

def get_venue_availability(venue_id, start_date, days=7):
    """Get venue availability for the next X days"""
    conn = get_db_connection()
    
    # Get all booked slots for this venue
    booked_slots = conn.execute('''
        SELECT match_date, time_slot 
        FROM matches 
        WHERE venue_id = ? AND match_date >= ?
        ORDER BY match_date, time_slot
    ''', (venue_id, start_date)).fetchall()
    
    # Convert to a set for easy lookup
    booked_set = {(slot['match_date'], slot['time_slot']) for slot in booked_slots}
    
    # Generate all possible slots for the next X days
    all_slots = []
    current_date = date.fromisoformat(start_date)
    
    for day in range(days):
        start_time = 9 * 60  # 9:00 AM
        end_time = 17 * 60   # 5:00 PM
        current_time = start_time
        
        while current_time + 60 <= end_time:  # 60-minute slots
            time_str = minutes_to_12h_format(current_time)
            slot_date = current_date.isoformat()
            
            is_available = (slot_date, time_str) not in booked_set
            all_slots.append({
                'date': slot_date,
                'time': time_str,
                'available': is_available
            })
            
            current_time += 90  # 60 min match + 30 min buffer
        
        current_date += timedelta(days=1)
    
    conn.close()
    return all_slots

def get_venue_usage_stats():
    """Get comprehensive venue usage statistics"""
    conn = get_db_connection()
    
    stats = conn.execute('''
        SELECT 
            V.id,
            V.name as venue_name,
            V.is_available,
            COUNT(M.id) as total_matches,
            COUNT(DISTINCT M.match_date) as days_used,
            MIN(M.match_date) as first_booking,
            MAX(M.match_date) as last_booking
        FROM venues V
        LEFT JOIN matches M ON V.id = M.venue_id
        GROUP BY V.id, V.name, V.is_available
        ORDER BY V.name
    ''').fetchall()
    
    # Convert to list of dictionaries to make them mutable
    stats_list = []
    for stat in stats:
        stat_dict = dict(stat)  # Convert Row to dictionary
        
        # Get upcoming bookings for each venue
        upcoming = conn.execute('''
            SELECT match_date, time_slot, E.name as event_name
            FROM matches M
            JOIN events E ON M.event_id = E.id
            WHERE M.venue_id = ? AND M.match_date >= date('now')
            ORDER BY M.match_date, M.time_slot
            LIMIT 5
        ''', (stat_dict['id'],)).fetchall()
        
        stat_dict['upcoming_bookings'] = [dict(booking) for booking in upcoming]
        stats_list.append(stat_dict)
    
    conn.close()
    return stats_list

def generate_fcfs_schedule(event_id, duration_type, venue_names):
    conn = get_db_connection()
    cursor = conn.cursor()

    cursor.execute('DELETE FROM matches WHERE event_id = ?', (event_id,))
    conn.commit()
    
    teams = cursor.execute('SELECT id, team_name FROM teams WHERE event_id = ?', (event_id,)).fetchall()
    
    venue_ids = []
    for name in venue_names:
        venue = cursor.execute('SELECT id FROM venues WHERE name = ?', (name,)).fetchone()
        if venue:
            venue_ids.append(venue['id'])

    event = cursor.execute('SELECT start_date FROM events WHERE id = ?', (event_id,)).fetchone()

    if len(teams) < 2:
        conn.close()
        return 'Not enough teams (min 2) to generate a schedule.'

    if not venue_ids:
        conn.close()
        return 'No valid venues found to generate a schedule. Please ensure the venue names are correct.'

    all_matches = []
    
    # Generate all unique team pairs (round-robin)
    team_pairs = []
    for i in range(len(teams)):
        for j in range(i + 1, len(teams)):
            team_pairs.append((teams[i]['id'], teams[j]['id']))
    
    random.shuffle(team_pairs)

    match_duration = int(duration_type)
    buffer_time = 30
    
    # Generate all possible slots (date + time + venue)
    all_possible_slots = []
    current_date = date.fromisoformat(event['start_date'])
    day_count = 0
    max_days = 30  # Maximum scheduling period
    
    while day_count < max_days:
        for venue_id in venue_ids:
            start_time = 9 * 60  # 9:00 AM in minutes (540 minutes)
            end_time = 17 * 60  # 5:00 PM in minutes (1020 minutes)
            
            current_time = start_time
            while current_time + match_duration <= end_time:
                # Convert to 12-hour format
                time_str = minutes_to_12h_format(current_time)
                all_possible_slots.append({
                    'date': current_date.isoformat(), 
                    'time': time_str, 
                    'venue_id': venue_id
                })
                current_time += match_duration + buffer_time

        current_date += timedelta(days=1)
        day_count += 1

    # Filter out occupied slots
    available_slots = []
    occupied_slots = []
    
    for slot in all_possible_slots:
        if is_venue_available(slot['venue_id'], slot['date'], slot['time'], conn):
            available_slots.append(slot)
        else:
            occupied_slots.append(slot)
    
    if len(available_slots) < len(team_pairs):
        conn.close()
        return f'Not enough available time slots. Needed: {len(team_pairs)}, Available: {len(available_slots)}. Please try different venues or extend event duration.'

    # Track team's last match time to avoid consecutive matches
    team_last_match = {}  # {team_id: {'date': date, 'time_index': int}}
    
    # Assign time indices to slots for comparison
    time_slots_sorted = sorted(available_slots, key=lambda x: (x['date'], x['time']))
    time_indices = {f"{slot['date']}_{slot['time']}": idx for idx, slot in enumerate(time_slots_sorted)}
    
    # Function to check if a team can play in a given slot (not consecutive)
    def can_team_play(team_id, slot):
        if team_id not in team_last_match:
            return True
        
        last_match = team_last_match[team_id]
        current_slot_index = time_indices[f"{slot['date']}_{slot['time']}"]
        last_slot_index = last_match['time_index']
        
        # Teams cannot play in consecutive slots
        return current_slot_index - last_slot_index > 1
    
    # Function to find the best available slot for a match
    def find_best_slot_for_match(team1_id, team2_id, used_slots):
        for slot in available_slots:
            slot_key = f"{slot['date']}_{slot['time']}_{slot['venue_id']}"
            
            if slot_key in used_slots:
                continue
            
            # Check if both teams can play in this slot (not consecutive matches)
            if (can_team_play(team1_id, slot) and 
                can_team_play(team2_id, slot) and
                is_venue_available(slot['venue_id'], slot['date'], slot['time'], conn)):
                return slot, slot_key
        
        # If no perfect slot found, try to find any available slot
        for slot in available_slots:
            slot_key = f"{slot['date']}_{slot['time']}_{slot['venue_id']}"
            
            if slot_key in used_slots:
                continue
            
            if is_venue_available(slot['venue_id'], slot['date'], slot['time'], conn):
                return slot, slot_key
        
        return None, None

    # Assign matches to slots with consecutive match prevention
    used_slots = set()
    scheduled_matches = 0
    
    for pair in team_pairs:
        team1_id, team2_id = pair
        
        slot, slot_key = find_best_slot_for_match(team1_id, team2_id, used_slots)
        
        if slot and slot_key:
            # Schedule the match
            all_matches.append((event_id, team1_id, team2_id, slot['time'], slot['date'], slot['venue_id']))
            used_slots.add(slot_key)
            scheduled_matches += 1
            
            # Update last match time for both teams
            slot_index = time_indices[f"{slot['date']}_{slot['time']}"]
            team_last_match[team1_id] = {'date': slot['date'], 'time_index': slot_index}
            team_last_match[team2_id] = {'date': slot['date'], 'time_index': slot_index}

    # If we couldn't schedule all matches due to consecutive match constraints,
    # try to schedule remaining matches without the constraint
    if scheduled_matches < len(team_pairs):
        remaining_pairs = [pair for pair in team_pairs if pair not in [(m[1], m[2]) for m in all_matches]]
        
        for pair in remaining_pairs:
            team1_id, team2_id = pair
            
            for slot in available_slots:
                slot_key = f"{slot['date']}_{slot['time']}_{slot['venue_id']}"
                
                if slot_key in used_slots:
                    continue
                
                if is_venue_available(slot['venue_id'], slot['date'], slot['time'], conn):
                    all_matches.append((event_id, team1_id, team2_id, slot['time'], slot['date'], slot['venue_id']))
                    used_slots.add(slot_key)
                    scheduled_matches += 1
                    break

    cursor.executemany('INSERT INTO matches (event_id, team1_id, team2_id, time_slot, match_date, venue_id) VALUES (?, ?, ?, ?, ?, ?)', all_matches)
    conn.commit()
    
    # Calculate statistics
    total_slots_needed = len(team_pairs)
    successfully_scheduled = scheduled_matches
    slots_with_constraint = len([m for m in all_matches if m[1] in team_last_match and m[2] in team_last_match])
    
    conn.close()
    
    return f'Schedule generated successfully with {successfully_scheduled} matches out of {total_slots_needed} needed. ' \
           f'Used {len(used_slots)} available slots out of {len(available_slots)} possible slots. ' \
           f'Consecutive match prevention applied to {slots_with_constraint} matches.'

def get_team_players(team_id):
    """Helper function to get players for a team"""
    conn = get_db_connection()
    players = conn.execute('SELECT * FROM players WHERE team_id = ?', (team_id,)).fetchall()
    conn.close()
    return players

# Make the function available to templates
@app.context_processor
def utility_processor():
    return dict(get_team_players=get_team_players)

# Add batch filter for Jinja2 templates
@app.template_filter('batch')
def batch_filter(seq, count):
    """Split sequence into batches of given count"""
    result = []
    for i in range(0, len(seq), count):
        result.append(seq[i:i + count])
    return result

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/register', methods=['GET', 'POST'])
def register():
    if request.method == 'POST':
        username = request.form['username']
        password = request.form['password']
        password_hash = generate_password_hash(password)
        conn = get_db_connection()
        try:
            conn.execute('INSERT INTO users (username, password_hash) VALUES (?, ?)', (username, password_hash))
            conn.commit()
            flash('Registration successful! Please log in.')
            return redirect(url_for('login'))
        except sqlite3.IntegrityError:
            flash('Username already exists.')
        finally:
            conn.close()
    return render_template('user/register.html')

@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        username = request.form['username']
        password = request.form['password']
        conn = get_db_connection()
        user = conn.execute('SELECT * FROM users WHERE username = ?', (username,)).fetchone()
        conn.close()
        if user and check_password_hash(user['password_hash'], password):
            session['user_id'] = user['id']
            session['username'] = user['username']
            session['is_admin'] = user['is_admin']
            flash('Login successful!')
            if user['is_admin']:
                return redirect(url_for('admin_dashboard'))
            else:
                return redirect(url_for('user_dashboard'))
        else:
            flash('Invalid username or password.')
    return render_template('user/login.html')

@app.route('/logout')
def logout():
    session.pop('user_id', None)
    session.pop('username', None)
    session.pop('is_admin', None)
    flash('You have been logged out.')
    return redirect(url_for('index'))

def is_admin():
    return session.get('is_admin', 0) == 1

@app.route('/admin')
def admin_dashboard():
    if not is_admin():
        flash('Unauthorized access.')
        return redirect(url_for('login'))
    
    conn = get_db_connection()
    events = conn.execute('SELECT * FROM events').fetchall()
    venues = conn.execute('SELECT * FROM venues').fetchall()
    
    # Get all teams with event and user information
    teams = conn.execute('''
        SELECT T.id, T.team_name, E.name AS event_name, U.username AS user_name
        FROM teams T
        JOIN events E ON T.event_id = E.id
        JOIN users U ON T.user_id = U.id
    ''').fetchall()
    
    # Get venue usage statistics
    venue_stats = get_venue_usage_stats()
    
    # Get all matches for all events
    matches = conn.execute('''
        SELECT T1.team_name AS team1_name, T2.team_name AS team2_name, M.time_slot, M.match_date, 
               V.name AS venue_name, E.name AS event_name
        FROM matches M
        JOIN teams T1 ON M.team1_id = T1.id
        JOIN teams T2 ON M.team2_id = T2.id
        JOIN venues V ON M.venue_id = V.id
        JOIN events E ON M.event_id = E.id
        ORDER BY M.match_date, 
        CASE 
            WHEN M.time_slot LIKE '%AM%' THEN 0
            WHEN M.time_slot LIKE '%PM%' THEN 1
            ELSE 2
        END,
        CAST(SUBSTR(M.time_slot, 1, INSTR(M.time_slot, ':') - 1) AS INTEGER),
        CAST(SUBSTR(M.time_slot, INSTR(M.time_slot, ':') + 1, 2) AS INTEGER)
    ''').fetchall()
    
    conn.close()
    return render_template('admin/admin_dashboard.html', events=events, venues=venues, 
                          matches=matches, teams=teams, venue_stats=venue_stats)

@app.route('/admin/statistics')
def statistics():
    if not is_admin():
        flash('Unauthorized access.')
        return redirect(url_for('login'))
    
    conn = get_db_connection()
    
    # Calculate date 30 days ago
    thirty_days_ago = (date.today() - timedelta(days=30)).isoformat()
    
    # User Statistics
    total_users = conn.execute('SELECT COUNT(*) as count FROM users').fetchone()['count']
    new_users = conn.execute('SELECT COUNT(*) as count FROM users WHERE id IN (SELECT id FROM users LIMIT -1 OFFSET 0)').fetchone()['count']
    active_users = conn.execute('SELECT COUNT(DISTINCT user_id) as count FROM teams WHERE user_id IS NOT NULL').fetchone()['count']
    admin_users = conn.execute('SELECT COUNT(*) as count FROM users WHERE is_admin = 1').fetchone()['count']
    regular_users = total_users - admin_users

    # Event Statistics
    total_events = conn.execute('SELECT COUNT(*) as count FROM events').fetchone()['count']
    active_events = conn.execute('SELECT COUNT(*) as count FROM events WHERE start_date <= date("now") AND start_date >= ?', (thirty_days_ago,)).fetchone()['count']
    completed_events = conn.execute('''
        SELECT COUNT(*) as count FROM events 
        WHERE start_date < date("now", "-30 days")
    ''').fetchone()['count']
    upcoming_events = conn.execute('''
        SELECT COUNT(*) as count FROM events 
        WHERE start_date > date("now")
    ''').fetchone()['count']
    events_with_schedules = conn.execute('''
        SELECT COUNT(DISTINCT event_id) as count FROM matches
    ''').fetchone()['count']

    # Team Statistics
    total_teams = conn.execute('SELECT COUNT(*) as count FROM teams').fetchone()['count']
    teams_last_month = conn.execute('''
        SELECT COUNT(*) as count FROM teams t
        JOIN events e ON t.event_id = e.id
        WHERE e.start_date >= ?
    ''', (thirty_days_ago,)).fetchone()['count']
    
    # Get teams per event
    teams_per_event = conn.execute('''
        SELECT e.name as event_name, COUNT(t.id) as team_count
        FROM events e
        LEFT JOIN teams t ON e.id = t.event_id
        GROUP BY e.id
        ORDER BY team_count DESC
        LIMIT 10
    ''').fetchall()

    # Player Statistics
    total_players = conn.execute('SELECT COUNT(*) as count FROM players').fetchone()['count']
    players_last_month = conn.execute('''
        SELECT COUNT(*) as count FROM players p
        JOIN teams t ON p.team_id = t.id
        JOIN events e ON t.event_id = e.id
        WHERE e.start_date >= ?
    ''', (thirty_days_ago,)).fetchone()['count']
    
    # Player management activity
    player_activity = conn.execute('''
        SELECT 
            (SELECT COUNT(*) FROM players) as total_players,
            (SELECT COUNT(*) FROM players p 
             JOIN teams t ON p.team_id = t.id 
             JOIN events e ON t.event_id = e.id 
             WHERE e.start_date >= ?) as new_players
    ''', (thirty_days_ago,)).fetchone()

    # Venue Statistics
    total_venues = conn.execute('SELECT COUNT(*) as count FROM venues').fetchone()['count']
    venue_usage = conn.execute('''
        SELECT v.name, COUNT(m.id) as match_count
        FROM venues v
        LEFT JOIN matches m ON v.id = m.venue_id
        GROUP BY v.id
        ORDER BY match_count DESC
    ''').fetchall()

    # Schedule Statistics
    total_matches = conn.execute('SELECT COUNT(*) as count FROM matches').fetchone()['count']
    matches_last_month = conn.execute('''
        SELECT COUNT(*) as count FROM matches 
        WHERE match_date >= ?
    ''', (thirty_days_ago,)).fetchone()['count']
    
    upcoming_matches = conn.execute('''
        SELECT COUNT(*) as count FROM matches 
        WHERE match_date >= date("now")
    ''').fetchone()['count']

    # Time slot distribution
    time_slots = conn.execute('''
        SELECT 
            COUNT(CASE WHEN time_slot LIKE '%AM%' THEN 1 END) as morning_matches,
            COUNT(CASE WHEN time_slot LIKE '%PM%' AND CAST(SUBSTR(time_slot, 1, INSTR(time_slot, ':') - 1) AS INTEGER) < 4 THEN 1 END) as afternoon_matches,
            COUNT(CASE WHEN time_slot LIKE '%PM%' AND CAST(SUBSTR(time_slot, 1, INSTR(time_slot, ':') - 1) AS INTEGER) >= 4 THEN 1 END) as evening_matches
        FROM matches
        WHERE match_date >= ?
    ''', (thirty_days_ago,)).fetchone()

    # Platform usage (simulated data - in real app, you'd track this)
    platform_usage = {
        'desktop': 35,
        'mobile': 65,
        'chrome': 60,
        'other_browsers': 40
    }

    # Recent activity
    recent_events = conn.execute('''
        SELECT name, start_date FROM events 
        ORDER BY start_date DESC 
        LIMIT 5
    ''').fetchall()

    conn.close()

    return render_template('admin/statistics.html',
                         # User stats
                         total_users=total_users,
                         new_users=new_users,
                         active_users=active_users,
                         admin_users=admin_users,
                         regular_users=regular_users,
                         
                         # Event stats
                         total_events=total_events,
                         active_events=active_events,
                         completed_events=completed_events,
                         upcoming_events=upcoming_events,
                         events_with_schedules=events_with_schedules,
                         
                         # Team stats
                         total_teams=total_teams,
                         teams_last_month=teams_last_month,
                         teams_per_event=teams_per_event,
                         
                         # Player stats
                         total_players=total_players,
                         players_last_month=players_last_month,
                         player_activity=player_activity,
                         
                         # Venue stats
                         total_venues=total_venues,
                         venue_usage=venue_usage,
                         
                         # Schedule stats
                         total_matches=total_matches,
                         matches_last_month=matches_last_month,
                         upcoming_matches=upcoming_matches,
                         time_slots=time_slots,
                         
                         # Other data
                         platform_usage=platform_usage,
                         recent_events=recent_events,
                         report_period=thirty_days_ago,
                         current_date=date.today())

@app.route('/admin/venue_availability')
def venue_availability():
    if not is_admin():
        flash('Unauthorized access.')
        return redirect(url_for('login'))
    
    conn = get_db_connection()
    venues = conn.execute('SELECT * FROM venues ORDER BY name').fetchall()
    conn.close()
    
    # Get availability for each venue for the next 7 days
    venue_availability_data = []
    start_date = date.today().isoformat()
    
    for venue in venues:
        availability = get_venue_availability(venue['id'], start_date, days=7)
        venue_availability_data.append({
            'venue': dict(venue),  # Convert to dict
            'availability': availability,
            'available_slots': len([slot for slot in availability if slot['available']]),
            'total_slots': len(availability)
        })
    
    return render_template('admin/venue_availability.html', 
                         venue_availability_data=venue_availability_data,
                         start_date=start_date)

@app.route('/admin/toggle_venue/<int:venue_id>', methods=['POST'])
def toggle_venue(venue_id):
    if not is_admin():
        flash('Unauthorized access.')
        return redirect(url_for('login'))
    
    conn = get_db_connection()
    venue = conn.execute('SELECT * FROM venues WHERE id = ?', (venue_id,)).fetchone()
    if venue:
        new_status = 0 if venue['is_available'] else 1
        conn.execute('UPDATE venues SET is_available = ? WHERE id = ?', (new_status, venue_id))
        conn.commit()
        status_text = "available" if new_status else "unavailable"
        flash(f'Venue "{venue["name"]}" marked as {status_text}.')
    
    conn.close()
    return redirect(url_for('admin_dashboard'))

@app.route('/admin/create_event', methods=['GET', 'POST'])
def create_event():
    if not is_admin():
        flash('Unauthorized access.')
        return redirect(url_for('login'))
    
    if request.method == 'POST':
        name = request.form['name']
        start_date = request.form['start_date']
        
        conn = get_db_connection()
        try:
            conn.execute('INSERT INTO events (name, start_date) VALUES (?, ?)', (name, start_date))
            conn.commit()
            flash('Event created successfully!')
            return render_template('admin/create_event.html')
        except Exception as e:
            flash(f'Error creating event: {str(e)}')
        finally:
            conn.close()
        
    return render_template('admin/create_event.html')

@app.route('/admin/delete_event/<int:event_id>', methods=['POST'])
def delete_event(event_id):
    if not is_admin():
        flash('Unauthorized access.')
        return redirect(url_for('login'))
    
    conn = get_db_connection()
    conn.execute('DELETE FROM teams WHERE event_id = ?', (event_id,))
    conn.execute('DELETE FROM matches WHERE event_id = ?', (event_id,))
    conn.execute('DELETE FROM events WHERE id = ?', (event_id,))
    conn.commit()
    conn.close()
    flash('Event and all associated data deleted successfully!')
    return redirect(url_for('admin_dashboard'))

@app.route('/admin/add_venue', methods=['POST'])
def add_venue():
    if not is_admin():
        flash('Unauthorized access.')
        return redirect(url_for('login'))
    
    venue_name = request.form['venue_name']
    conn = get_db_connection()
    try:
        conn.execute('INSERT INTO venues (name) VALUES (?)', (venue_name,))
        conn.commit()
        flash(f'Venue "{venue_name}" added successfully!')
    except sqlite3.IntegrityError:
        flash(f'Venue "{venue_name}" already exists.')
    finally:
        conn.close()
    
    return redirect(url_for('admin_dashboard'))

@app.route('/admin/manage_teams/<int:event_id>')
def manage_teams(event_id):
    if not is_admin():
        flash('Unauthorized access.')
        return redirect(url_for('login'))
    
    conn = get_db_connection()
    teams = conn.execute('SELECT T.id, T.team_name, U.username FROM teams T JOIN users U ON T.user_id = U.id WHERE T.event_id = ?', (event_id,)).fetchall()
    event = conn.execute('SELECT * FROM events WHERE id = ?', (event_id,)).fetchone()
    venues = conn.execute('SELECT * FROM venues').fetchall()
    conn.close()
    
    return render_template('admin/manage_teams.html', teams=teams, event=event, venues=venues)

@app.route('/admin/delete_team/<int:team_id>', methods=['POST'])
def delete_team(team_id):
    if not is_admin():
        flash('Unauthorized access.')
        return redirect(url_for('login'))
    
    conn = get_db_connection()
    team = conn.execute('SELECT event_id FROM teams WHERE id = ?', (team_id,)).fetchone()
    if not team:
        flash('Team not found.')
        conn.close()
        return redirect(url_for('admin_dashboard'))

    event_id = team['event_id']
    
    conn.execute('DELETE FROM players WHERE team_id = ?', (team_id,))
    conn.execute('DELETE FROM matches WHERE team1_id = ? OR team2_id = ?', (team_id, team_id))
    conn.execute('DELETE FROM teams WHERE id = ?', (team_id,))
    conn.commit()
    conn.close()
    flash('Team and all associated data deleted successfully!')
    return redirect(url_for('manage_teams', event_id=event_id))

@app.route('/admin/generate_schedule/<int:event_id>', methods=['GET', 'POST'])
def generate_schedule(event_id):
    if not is_admin():
        flash('Unauthorized access.')
        return redirect(url_for('login'))

    conn = get_db_connection()
    event = conn.execute('SELECT * FROM events WHERE id = ?', (event_id,)).fetchone()

    if request.method == 'POST':
        venue_name = request.form.get('venue_name')
        duration = request.form.get('duration')
        
        if not venue_name or not duration:
            flash('Please fill in all required fields.')
            venues = conn.execute('SELECT * FROM venues WHERE is_available = 1').fetchall()
            conn.close()
            return render_template('admin/generate_schedule.html', event=event, venues=venues)
        
        try:
            duration_int = int(duration)
            if duration_int < 30 or duration_int > 180:
                flash('Duration must be between 30 and 180 minutes.')
                venues = conn.execute('SELECT * FROM venues WHERE is_available = 1').fetchall()
                conn.close()
                return render_template('admin/generate_schedule.html', event=event, venues=venues)
        except ValueError:
            flash('Invalid duration value. Please enter a number.')
            venues = conn.execute('SELECT * FROM venues WHERE is_available = 1').fetchall()
            conn.close()
            return render_template('admin/generate_schedule.html', event=event, venues=venues)
        
        message = generate_fcfs_schedule(event_id, duration, [venue_name])
        flash(message)
        conn.close()
        return redirect(url_for('view_schedule', event_id=event_id))
    
    venues = conn.execute('SELECT * FROM venues WHERE is_available = 1').fetchall()
    conn.close()
    
    if not venues:
        flash('No venues available. Please add venues first.')
        return redirect(url_for('admin_dashboard'))
    
    return render_template('admin/generate_schedule.html', event=event, venues=venues)
    
@app.route('/admin/view_schedule/<int:event_id>')
def view_schedule(event_id):
    if not is_admin():
        flash('Unauthorized access.')
        return redirect(url_for('login'))
        
    conn = get_db_connection()
    event = conn.execute('SELECT * FROM events WHERE id = ?', (event_id,)).fetchone()
    if not event:
        flash('Event not found.')
        return redirect(url_for('admin_dashboard'))
    
    matches = conn.execute('''
        SELECT T1.team_name AS team1_name, T2.team_name AS team2_name, M.time_slot, M.match_date, V.name AS venue_name
        FROM matches M
        JOIN teams T1 ON M.team1_id = T1.id
        JOIN teams T2 ON M.team2_id = T2.id
        JOIN venues V ON M.venue_id = V.id
        WHERE M.event_id = ?
        ORDER BY M.match_date, 
        CASE 
            WHEN M.time_slot LIKE '%AM%' THEN 0
            WHEN M.time_slot LIKE '%PM%' THEN 1
            ELSE 2
        END,
        CAST(SUBSTR(M.time_slot, 1, INSTR(M.time_slot, ':') - 1) AS INTEGER),
        CAST(SUBSTR(M.time_slot, INSTR(M.time_slot, ':') + 1, 2) AS INTEGER)
    ''', (event_id,)).fetchall()
    
    conn.close()
    return render_template('admin/view_schedule.html', event=event, matches=matches)

@app.route('/admin/team_players/<int:team_id>')
def view_team_players(team_id):
    if not is_admin():
        flash('Unauthorized access.')
        return redirect(url_for('login'))
    
    conn = get_db_connection()
    team = conn.execute('''
        SELECT T.*, E.name as event_name, U.username 
        FROM teams T 
        JOIN events E ON T.event_id = E.id 
        JOIN users U ON T.user_id = U.id 
        WHERE T.id = ?
    ''', (team_id,)).fetchone()
    
    players = conn.execute('SELECT * FROM players WHERE team_id = ?', (team_id,)).fetchall()
    conn.close()
    
    return render_template('admin/team_players.html', team=dict(team), players=players)

@app.route('/admin/delete_player/<int:player_id>', methods=['POST'])
def admin_delete_player(player_id):
    if not is_admin():
        flash('Unauthorized access.')
        return redirect(url_for('login'))

    conn = get_db_connection()
    player = conn.execute('SELECT * FROM players WHERE id = ?', (player_id,)).fetchone()
    if not player:
        flash('Player not found.')
        conn.close()
        return redirect(url_for('admin_dashboard'))
    
    team_id = player['team_id']
    
    conn.execute('DELETE FROM players WHERE id = ?', (player_id,))
    conn.commit()
    conn.close()
    
    flash('Player deleted successfully.')
    return redirect(url_for('view_team_players', team_id=team_id))

@app.route('/user/dashboard')
def user_dashboard():
    if 'user_id' not in session:
        return redirect(url_for('login'))
        
    conn = get_db_connection()
    events = conn.execute('SELECT * FROM events').fetchall()
    
    user_teams = conn.execute('SELECT T.*, E.name AS event_name FROM teams T JOIN events E ON T.event_id = E.id WHERE T.user_id = ?', (session['user_id'],)).fetchall()
    
    matches = []
    if user_teams:
        user_team_ids = [team['id'] for team in user_teams]
        matches_query = '''
            SELECT T1.team_name AS team1_name, T2.team_name AS team2_name, M.time_slot, M.match_date, V.name AS venue_name
            FROM matches M
            JOIN teams T1 ON M.team1_id = T1.id
            JOIN teams T2 ON M.team2_id = T2.id
            JOIN venues V ON M.venue_id = V.id
            WHERE M.team1_id IN ({}) OR M.team2_id IN ({})
            ORDER BY M.match_date, 
            CASE 
                WHEN M.time_slot LIKE '%AM%' THEN 0
                WHEN M.time_slot LIKE '%PM%' THEN 1
            ELSE 2
            END,
            CAST(SUBSTR(M.time_slot, 1, INSTR(M.time_slot, ':') - 1) AS INTEGER),
            CAST(SUBSTR(M.time_slot, INSTR(M.time_slot, ':') + 1, 2) AS INTEGER)
        '''.format(','.join('?'*len(user_team_ids)), ','.join('?'*len(user_team_ids)))
        
        matches = conn.execute(matches_query, user_team_ids + user_team_ids).fetchall()

    conn.close()
    
    return render_template('user/user_dashboard.html', events=events, user_teams=user_teams, matches=matches)

@app.route('/user/register_event/<int:event_id>', methods=['GET', 'POST'])
def register_event(event_id):
    if 'user_id' not in session:
        return redirect(url_for('login'))
    
    conn = get_db_connection()
    event = conn.execute('SELECT * FROM events WHERE id = ?', (event_id,)).fetchone()
    
    if request.method == 'POST':
        team_name = request.form['team_name']
        existing_team = conn.execute('SELECT id FROM teams WHERE team_name = ? AND event_id = ?', (team_name, event_id)).fetchone()
        if existing_team:
            flash(f'Team name "{team_name}" already exists for this event. Please choose a different name.')
        else:
            conn.execute('INSERT INTO teams (team_name, user_id, event_id) VALUES (?, ?, ?)', (team_name, session['user_id'], event_id))
            conn.commit()
            flash(f'Team "{team_name}" registered for {event["name"]}!')
            return render_template('user/register_event.html', event=event)
    
    conn.close()
    return render_template('user/register_event.html', event=event)

@app.route('/user/add_player/<int:team_id>', methods=['GET', 'POST'])
def add_player(team_id):
    if 'user_id' not in session:
        flash('Unauthorized access.')
        return redirect(url_for('login'))

    conn = get_db_connection()
    
    if session.get('is_admin'):
        # Admin can add players to any team
        team = conn.execute('SELECT * FROM teams WHERE id = ?', (team_id,)).fetchone()
    else:
        # Regular users can only add players to their own teams
        team = conn.execute('SELECT * FROM teams WHERE id = ? AND user_id = ?', (team_id, session['user_id'])).fetchone()

    if not team:
        flash('Team not found or you do not have permission to edit it.')
        conn.close()
        return redirect(url_for('user_dashboard' if not session.get('is_admin') else 'admin_dashboard'))

    if request.method == 'POST':
        player_name = request.form['player_name']
        existing_player = conn.execute('SELECT id FROM players WHERE player_name = ? AND team_id = ?', (player_name, team_id)).fetchone()
        if existing_player:
            flash(f'Player "{player_name}" already exists in this team.')
        else:
            conn.execute('INSERT INTO players (player_name, team_id) VALUES (?, ?)', (player_name, team_id))
            conn.commit()
            flash(f'Player "{player_name}" added to {team["team_name"]}.')
            
            # Redirect based on user type
            if session.get('is_admin'):
                players = conn.execute('SELECT * FROM players WHERE team_id = ?', (team_id,)).fetchall()
                conn.close()
                return redirect(url_for('view_team_players', team_id=team_id))
            else:
                players = conn.execute('SELECT * FROM players WHERE team_id = ?', (team_id,)).fetchall()
                conn.close()
                return render_template('user/add_player.html', team=team, players=players)

    players = conn.execute('SELECT * FROM players WHERE team_id = ?', (team_id,)).fetchall()
    conn.close()
    
    if session.get('is_admin'):
        return redirect(url_for('view_team_players', team_id=team_id))
    else:
        return render_template('user/add_player.html', team=team, players=players)

@app.route('/user/delete_player/<int:player_id>', methods=['POST'])
def delete_player(player_id):
    if 'user_id' not in session:
        flash('Unauthorized access.')
        return redirect(url_for('login'))

    conn = get_db_connection()
    player = conn.execute('SELECT * FROM players WHERE id = ?', (player_id,)).fetchone()
    if not player:
        flash('Player not found.')
        conn.close()
        return redirect(url_for('user_dashboard'))
    
    team_id = player['team_id']
    
    if session.get('is_admin'):
        # Admin can delete any player
        team = conn.execute('SELECT * FROM teams WHERE id = ?', (team_id,)).fetchone()
    else:
        # Regular users can only delete players from their own teams
        team = conn.execute('SELECT * FROM teams WHERE id = ? AND user_id = ?', (team_id, session['user_id'])).fetchone()
    
    if not team:
        flash('You do not have permission to delete this player.')
        conn.close()
        return redirect(url_for('user_dashboard'))
        
    conn.execute('DELETE FROM players WHERE id = ?', (player_id,))
    conn.commit()
    conn.close()
    flash('Player deleted successfully.')
    
    if session.get('is_admin'):
        return redirect(url_for('view_team_players', team_id=team_id))
    else:
        return redirect(url_for('add_player', team_id=team_id))

@app.route('/user/delete_team/<int:team_id>', methods=['POST'])
def delete_user_team(team_id):
    if 'user_id' not in session:
        flash('Unauthorized access.')
        return redirect(url_for('login'))

    conn = get_db_connection()
    
    # Check if the team belongs to the current user
    team = conn.execute('SELECT * FROM teams WHERE id = ? AND user_id = ?', (team_id, session['user_id'])).fetchone()
    
    if not team:
        flash('Team not found or you do not have permission to delete it.')
        conn.close()
        return redirect(url_for('user_dashboard'))

    try:
        # Get team name for flash message
        team_name = team['team_name']
        
        # Delete all players associated with the team
        conn.execute('DELETE FROM players WHERE team_id = ?', (team_id,))
        
        # Delete all matches involving this team
        conn.execute('DELETE FROM matches WHERE team1_id = ? OR team2_id = ?', (team_id, team_id))
        
        # Delete the team itself
        conn.execute('DELETE FROM teams WHERE id = ?', (team_id,))
        
        conn.commit()
        flash(f'Team "{team_name}" and all associated players have been deleted successfully!')
        
    except Exception as e:
        conn.rollback()
        flash(f'Error deleting team: {str(e)}')
    
    finally:
        conn.close()
    
    return redirect(url_for('user_dashboard'))

if __name__ == '__main__':
    app.run(debug=True)
