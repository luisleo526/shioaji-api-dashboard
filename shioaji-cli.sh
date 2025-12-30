#!/bin/bash
#
# Shioaji Auto-Trading CLI
# A user-friendly command-line interface for managing the trading system
#
# Usage: ./shioaji-cli.sh [command]
#

set -e

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
CYAN='\033[0;36m'
NC='\033[0m' # No Color

# Project name (for docker volume names)
PROJECT_NAME="shioaji-api-dashboard"

# Print colored output
print_header() {
    echo ""
    echo -e "${CYAN}════════════════════════════════════════════════════════════${NC}"
    echo -e "${CYAN}  📈 Shioaji Auto-Trading System${NC}"
    echo -e "${CYAN}════════════════════════════════════════════════════════════${NC}"
    echo ""
}

print_success() {
    echo -e "${GREEN}✅ $1${NC}"
}

print_warning() {
    echo -e "${YELLOW}⚠️  $1${NC}"
}

print_error() {
    echo -e "${RED}❌ $1${NC}"
}

print_info() {
    echo -e "${BLUE}ℹ️  $1${NC}"
}

# Check if .env file exists
check_env() {
    if [ ! -f ".env" ]; then
        print_error ".env file not found!"
        echo ""
        echo "Please create .env file first:"
        echo "  cp example.env .env"
        echo "  # Then edit .env with your API keys"
        echo ""
        exit 1
    fi
}

# Show help
show_help() {
    print_header
    echo "Usage: ./shioaji-cli.sh [command]"
    echo ""
    echo "Commands:"
    echo -e "  ${GREEN}start${NC}        Start all services"
    echo -e "  ${GREEN}stop${NC}         Stop all services"
    echo -e "  ${GREEN}restart${NC}      Restart all services"
    echo -e "  ${GREEN}status${NC}       Show service status"
    echo -e "  ${GREEN}logs${NC}         Show all logs (follow mode)"
    echo -e "  ${GREEN}logs-api${NC}     Show API logs only"
    echo -e "  ${GREEN}logs-worker${NC}  Show Trading Worker logs only"
    echo -e "  ${GREEN}health${NC}       Check system health"
    echo -e "  ${GREEN}dashboard${NC}    Open dashboard in browser"
    echo -e "  ${GREEN}reset${NC}        Reset database (DELETE ALL DATA)"
    echo -e "  ${GREEN}update${NC}       Pull latest code and rebuild"
    echo -e "  ${GREEN}help${NC}         Show this help message"
    echo ""
    echo "Examples:"
    echo "  ./shioaji-cli.sh start      # Start the trading system"
    echo "  ./shioaji-cli.sh logs       # View live logs"
    echo "  ./shioaji-cli.sh status     # Check if services are running"
    echo ""
}

# Start services
cmd_start() {
    print_header
    check_env
    print_info "Starting services..."
    echo ""
    docker compose up -d
    echo ""
    print_success "Services started!"
    echo ""
    echo "Dashboard: http://localhost:9879/dashboard"
    echo "API Docs:  http://localhost:9879/docs"
    echo ""
    print_info "Waiting for services to be ready..."
    sleep 5
    cmd_health_quiet
}

# Stop services
cmd_stop() {
    print_header
    print_info "Stopping services..."
    echo ""
    docker compose down
    echo ""
    print_success "Services stopped!"
}

# Restart services
cmd_restart() {
    print_header
    print_info "Restarting services..."
    echo ""
    docker compose restart
    echo ""
    print_success "Services restarted!"
    sleep 3
    cmd_health_quiet
}

# Show status
cmd_status() {
    print_header
    print_info "Service Status:"
    echo ""
    docker compose ps
    echo ""
}

# Show logs
cmd_logs() {
    print_header
    print_info "Showing logs (Ctrl+C to exit)..."
    echo ""
    docker compose logs -f
}

# Show API logs
cmd_logs_api() {
    print_header
    print_info "Showing API logs (Ctrl+C to exit)..."
    echo ""
    docker compose logs -f api
}

# Show worker logs
cmd_logs_worker() {
    print_header
    print_info "Showing Trading Worker logs (Ctrl+C to exit)..."
    echo ""
    docker compose logs -f trading-worker
}

# Health check (quiet version)
cmd_health_quiet() {
    local health_url="http://localhost:9879/health"
    local max_attempts=10
    local attempt=1
    
    while [ $attempt -le $max_attempts ]; do
        response=$(curl -s "$health_url" 2>/dev/null || echo "")
        if [ -n "$response" ]; then
            api_status=$(echo "$response" | grep -o '"api":"[^"]*"' | cut -d'"' -f4)
            worker_status=$(echo "$response" | grep -o '"trading_worker":"[^"]*"' | cut -d'"' -f4)
            redis_status=$(echo "$response" | grep -o '"redis":"[^"]*"' | cut -d'"' -f4)
            
            echo ""
            echo "System Health:"
            if [ "$api_status" = "healthy" ]; then
                echo -e "  API:            ${GREEN}● healthy${NC}"
            else
                echo -e "  API:            ${RED}● $api_status${NC}"
            fi
            if [ "$worker_status" = "healthy" ]; then
                echo -e "  Trading Worker: ${GREEN}● healthy${NC}"
            else
                echo -e "  Trading Worker: ${YELLOW}● $worker_status${NC}"
            fi
            if [ "$redis_status" = "connected" ]; then
                echo -e "  Redis:          ${GREEN}● connected${NC}"
            else
                echo -e "  Redis:          ${RED}● $redis_status${NC}"
            fi
            echo ""
            return 0
        fi
        sleep 2
        attempt=$((attempt + 1))
    done
    
    print_warning "Could not reach health endpoint. Services may still be starting..."
    echo ""
}

# Health check
cmd_health() {
    print_header
    print_info "Checking system health..."
    cmd_health_quiet
}

# Open dashboard
cmd_dashboard() {
    local url="http://localhost:9879/dashboard"
    print_header
    print_info "Opening dashboard..."
    echo ""
    echo "URL: $url"
    echo ""
    
    # Try to open browser
    if command -v xdg-open &> /dev/null; then
        xdg-open "$url" 2>/dev/null &
    elif command -v open &> /dev/null; then
        open "$url" 2>/dev/null &
    else
        print_warning "Could not open browser automatically."
        echo "Please open this URL in your browser: $url"
    fi
}

# Reset database
cmd_reset() {
    print_header
    print_warning "DATABASE RESET"
    echo ""
    echo "This will:"
    echo "  1. Stop all services"
    echo "  2. Delete all order history"
    echo "  3. Delete Redis cache"
    echo "  4. Restart with fresh database"
    echo ""
    print_error "ALL DATA WILL BE PERMANENTLY DELETED!"
    echo ""
    read -p "Type 'yes' to confirm: " confirm
    
    if [ "$confirm" != "yes" ]; then
        echo ""
        print_info "Cancelled."
        exit 0
    fi
    
    echo ""
    print_info "Stopping services..."
    docker compose down
    
    echo ""
    print_info "Removing data volumes..."
    docker volume rm ${PROJECT_NAME}_postgres_data 2>/dev/null || true
    docker volume rm ${PROJECT_NAME}_redis_data 2>/dev/null || true
    
    echo ""
    print_info "Starting services with fresh database..."
    docker compose up -d
    
    echo ""
    print_info "Waiting for database migration..."
    sleep 10
    
    # Check migration logs
    echo ""
    echo "Migration logs:"
    docker compose logs db-migrate | tail -20
    
    echo ""
    print_success "Database reset complete!"
    echo ""
    cmd_health_quiet
}

# Update and rebuild
cmd_update() {
    print_header
    print_info "Updating system..."
    echo ""
    
    echo "Pulling latest code..."
    git pull
    
    echo ""
    echo "Rebuilding containers..."
    docker compose build
    
    echo ""
    echo "Restarting services..."
    docker compose up -d
    
    echo ""
    print_success "Update complete!"
    sleep 3
    cmd_health_quiet
}

# Main
case "${1:-help}" in
    start)
        cmd_start
        ;;
    stop)
        cmd_stop
        ;;
    restart)
        cmd_restart
        ;;
    status)
        cmd_status
        ;;
    logs)
        cmd_logs
        ;;
    logs-api)
        cmd_logs_api
        ;;
    logs-worker)
        cmd_logs_worker
        ;;
    health)
        cmd_health
        ;;
    dashboard)
        cmd_dashboard
        ;;
    reset)
        cmd_reset
        ;;
    update)
        cmd_update
        ;;
    help|--help|-h)
        show_help
        ;;
    *)
        print_error "Unknown command: $1"
        show_help
        exit 1
        ;;
esac

