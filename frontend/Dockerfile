# tradingweb/frontend/Dockerfile

FROM node:24-alpine

WORKDIR /app

# Change ownership of the workdir to the 'node' user
# (The 'node' user comes pre-installed in the node alpine image)
RUN chown -R node:node /app

# Switch to 'node' user
USER node

# Install dependencies first (for caching)
COPY --chown=node:node package*.json ./
RUN npm install

COPY --chown=node:node . .

EXPOSE 4321
ENV HOST=0.0.0.0

CMD ["npm", "run", "dev"]